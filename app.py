from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from replenishment.demo import demo_dataset
from replenishment.agent import AgentResult, run_agent
from replenishment.agent_client import model_from_env
from replenishment.backtest import backtest_dataset
from replenishment.engine import calculate_orders
from replenishment.importer import load_dataset
from replenishment.models import Dataset, Policy, StockInput
from replenishment.review import export_csv, fingerprint
from replenishment.scenarios import SCENARIOS, training_scenario

log = logging.getLogger("replenishment.ui")
st.set_page_config(page_title="Пополнение склада", layout="wide")
st.title("Заказ без разовых всплесков")
st.markdown(
    "**Для менеджера закупа:** проверьте, что пополнить, почему предложено это количество, "
    "и утвердите проект заказа. **Сервис не отправляет заказ поставщику.**"
)
st.caption("Продажи + доступный запас + товары в пути → расчёт → проверка по поставщику → утверждение")


def reset_agent():
    for name in ("agent_result", "agent_approval", "agent_identity"):
        st.session_state.pop(name, None)


def reset_calculation():
    st.session_state.pop("calculation", None)
    st.session_state.pop("approval", None)
    reset_agent()


def show_agent(dataset, calculation, dataset_version):
    st.subheader("AI-помощник по одной позиции")
    st.caption("Задайте вопрос о расчёте. Проект одной позиции требует отдельного утверждения. "
               "Данные партнёра во внешний AI не передаются; не вводите персональные данные и секреты в вопрос.")
    if dataset.synthetic:
        st.caption("SYNTHETIC/TRAINING — AI работает с учебным примером.")
    rows = {row.key: row for row in calculation.rows}
    if not rows:
        reset_agent()
        return
    item_key = st.selectbox("Позиция для AI", list(rows), key="agent_item",
                            format_func=lambda key: f"{rows[key].supplier} · {rows[key].sku} — {rows[key].name}")
    question = st.text_input("Вопрос AI", key="agent_question", max_chars=4000)
    request_order = st.checkbox("Подготовить проект заказа", key="agent_request_order")
    deep_analysis = st.checkbox("Усиленное AI-пояснение", key="agent_deep_analysis")
    identity = hashlib.sha256(json.dumps([
        dataset_version, calculation.model_dump_json(), item_key, question, request_order, deep_analysis,
    ], ensure_ascii=False).encode()).hexdigest()
    if st.session_state.get("agent_identity") != identity:
        reset_agent()
        st.session_state.agent_identity = identity
    if st.button("Запросить AI-пояснение", key="agent_submit", disabled=not question.strip()):
        reset_agent()
        st.session_state.agent_identity = identity
        model = None
        with st.spinner("AI проверяет выбранную позицию…"):
            try:
                model = model_from_env(complex_task=request_order or deep_analysis) if dataset.synthetic else None
                result = run_agent(dataset, calculation.policy, item_key, question,
                                   request_order=request_order, model=model, allow_partner_data=False)
            except Exception:
                result = AgentResult(status="model_unavailable", synthetic=dataset.synthetic,
                                     message="AI недоступен. Проверьте настройки подключения. Локальный расчёт доступен.")
            finally:
                if model is not None:
                    try:
                        model.close()
                    except Exception:
                        log.warning("agent_client_close_failed")
        st.session_state.agent_result = result

    result = st.session_state.get("agent_result")
    if result is None:
        return
    statuses = {
        "ok": "Пояснение подготовлено", "pending_approval": "Проект ожидает вашего утверждения",
        "needs_data": "Недостаточно данных", "invalid_data": "Ошибки во входных данных",
        "not_found": "Позиция не найдена", "model_error": "AI не завершил анализ",
        "tool_error": "Ошибка инструмента агента", "model_unavailable": "AI не подключён",
        "data_not_authorized": "Передача данных партнёра в AI запрещена",
    }
    successful = result.status in ("ok", "pending_approval")
    (st.info if successful else st.warning)(f"{statuses[result.status]} ({result.status}). {result.message}")
    row = result.recommendation
    if row is not None:
        st.write(f"Статус локального расчёта: {row.status}")
        if row.status == "ok" and row.quantity is not None:
            st.metric("Проверенное количество по расчёту", f"{row.quantity:g} {row.unit or ''}")
        else:
            st.write(row.explanation)
    if not successful:
        st.session_state.pop("agent_approval", None)
        return
    pending = result.pending_order
    if not request_order:
        st.session_state.pop("agent_approval", None)
        st.info("Подготовка AI-проекта заказа не запрошена. Утверждать и скачивать нечего.")
    elif result.status != "pending_approval" or pending is None:
        st.session_state.pop("agent_approval", None)
        st.warning("AI-проект заказа не подготовлен. Утверждение и CSV недоступны.")
    else:
        st.markdown("**Отдельный AI-проект: одна выбранная позиция**")
        try:
            if (len(pending.calculation.rows) != 1 or pending.calculation.rows[0].key != item_key
                    or set(pending.quantities) != {item_key} or pending.quantities[item_key] <= 0):
                raise ValueError("Unexpected scope")
            token = fingerprint(pending.calculation, pending.quantities)
            if st.session_state.get("agent_approval") != token:
                st.session_state.pop("agent_approval", None)
            st.write(f"{rows[item_key].supplier} · {rows[item_key].sku}: "
                     f"{pending.quantities[item_key]:g} {pending.calculation.rows[0].unit or ''}")
            approve, reject = st.columns(2)
            if approve.button("Approve — утвердить AI-проект", key="agent_approve"):
                st.session_state.agent_approval = token
            if reject.button("Reject — снять утверждение AI-проекта", key="agent_reject"):
                st.session_state.pop("agent_approval", None)
            if st.session_state.get("agent_approval") == token:
                st.success("AI-проект одной позиции утверждён. Заказ поставщику не отправлялся.")
                st.download_button("Скачать утверждённый AI-проект CSV",
                                   export_csv(pending.calculation, pending.quantities, st.session_state.agent_approval),
                                   file_name="synthetic_agent_order.csv" if result.synthetic else "agent_order.csv",
                                   mime="text/csv", key="agent_download")
            else:
                st.warning("AI-проект не утверждён. CSV станет доступен только после Approve.")
        except (ValueError, TypeError):
            st.session_state.pop("agent_approval", None)
            st.error("AI-проект некорректен. Выполните запрос заново; утверждение и экспорт недоступны.")
            return
    if result.narrative:
        with st.expander("AI-пояснение модели (непроверенный текст)"):
            st.caption("Текст модели может ошибаться. Статус утверждения, количество и доступность CSV указаны выше по проверенному расчёту и вашему действию.")
            st.text(result.narrative)


@st.cache_data(show_spinner=False, max_entries=2)
def read_sources(paths: tuple[str, ...], signature: tuple):
    return load_dataset([Path(p) for p in paths])


mode = st.radio("Данные", ["Учебный пример", "Файлы партнёра"], horizontal=True)
if st.session_state.get("mode") != mode:
    st.session_state.mode = mode
    st.session_state.pop("dataset", None)
    st.session_state["stocks"] = {}
    st.session_state["stockouts"] = {}
    st.session_state["growth"] = {}
    reset_calculation()

overview = st.empty()
overview.info("Результатов пока нет. Выберите данные и нажмите «Рассчитать рекомендации».")
scenario_stockouts = {}
if mode == "Учебный пример":
    st.warning("SYNTHETIC/TRAINING — вымышленные данные. Этот режим проверяет поведение, а не фактическую потребность компании.")
    scenario = st.selectbox("Учебный сценарий", SCENARIOS, key="training_scenario")
    if st.session_state.get("active_scenario") != scenario:
        st.session_state.active_scenario = scenario
        st.session_state.stocks, st.session_state.stockouts, st.session_state.growth = {}, {}, {}
        reset_calculation()
    if scenario == SCENARIOS[0]:
        dataset = demo_dataset()
        st.caption("Сравните REGULAR и SPIKE: разовая часть 5000 показана отдельно от регулярного спроса.")
    else:
        dataset, scenario_stockouts, scenario_description = training_scenario(scenario)
        st.caption(scenario_description)
else:
    default_paths = [Path.home() / "OneDrive" / "Desktop" / name for name in ("Хакатон, файлы", "Хакатон файлы 2")]
    paths_text = st.text_area("Папки с Excel, по одной на строку", value="\n".join(str(p) for p in default_paths if p.exists()))
    paths = tuple(line.strip().strip('"') for line in paths_text.splitlines() if line.strip())
    try:
        files = sorted({p for entry in paths for p in (Path(entry).rglob("*.xlsx") if Path(entry).is_dir() else [Path(entry)]) if p.exists()})
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    except OSError:
        signature = ()
        st.error("Не удалось прочитать выбранную папку. Проверьте доступ.")
    identity = (paths, signature)
    if st.session_state.get("source_identity") != identity:
        st.session_state.pop("dataset", None)
        st.session_state["stocks"], st.session_state["stockouts"], st.session_state["growth"] = {}, {}, {}
        reset_calculation()
    if st.button("Загрузить источники", disabled=not paths):
        with st.spinner("Читаю Excel и сопоставляю источники…"):
            try:
                st.session_state.dataset = read_sources(paths, signature)
                st.session_state.source_identity = identity
                reset_calculation()
            except Exception as exc:
                log.error("Import failure: %s", type(exc).__name__)
                st.error("Загрузка не выполнена. Проверьте формат и доступность файлов.")
    dataset = st.session_state.get("dataset")
    if dataset is None:
        st.info("Загрузите выданные файлы для расчёта.")
        st.stop()

dataset_version = hashlib.sha256(dataset.model_dump_json().encode()).hexdigest()
if st.session_state.get("dataset_version") != dataset_version:
    reset_calculation()
    st.session_state.dataset_version = dataset_version
for issue in dataset.issues:
    st.info(issue)
if not dataset.items:
    st.error("Нет доступных товаров. Проверьте источники.")
    st.stop()
with st.expander("Проверка источников и охват данных"):
    st.dataframe(pd.DataFrame([s.model_dump() for s in dataset.sources]), hide_index=True)
    st.write(f"Товаров в объединении: {len(dataset.items)}")
    st.caption("Пустые продажи не заменяются нулями. Месячные остатки не доказывают дни отсутствия товара. Клиентская группировка доступна только при обезличенном ID.")

dates = [i.stock_date for i in dataset.items if i.stock_date]
as_of = st.date_input("Дата среза для расчёта", value=max(dates) if dates else date.today())
st.caption("Горизонт и срок поставки — параметры менеджера. В учебном примере они заданы для демонстрации.")
a, b, c = st.columns(3)
horizon = a.number_input("Период между закупками, дней", min_value=1, max_value=366, value=30 if dataset.synthetic else None)
lead = b.number_input("Срок новой поставки, дней", min_value=0, max_value=366, value=7 if dataset.synthetic else None)
suppliers = c.multiselect("Поставщики", sorted({i.supplier for i in dataset.items}), default=[])
categories = st.multiselect("Категории (исходные коды)", sorted({i.category or "Без категории" for i in dataset.items}), default=[])
category_horizons = {}
with st.expander("Период закупки по категориям"):
    st.caption("Необязательное явное правило менеджера: для выбранных категорий заменяет общий период. "
               "Смысл исходных кодов категорий не предполагается. Срок поставки остаётся общим.")
    category_options = sorted({i.category for i in dataset.items if i.category is not None})
    custom_categories = st.multiselect("Категории с отдельным периодом", category_options,
                                      key="category_rules_" + dataset_version[:16])
    for category in custom_categories:
        value = st.number_input(f"Период закупки для категории {category}, дней", min_value=1, max_value=366,
                                value=int(horizon or 30),
                                key="category_days_" + dataset_version[:16] + "_" + category)
        category_horizons[category] = value

with st.expander("Подтверждённые уточнения менеджера"):
    st.caption("Вводите только подтверждённые значения. Они заменяют соответствующий вход и сохраняются в текущей сессии.")
    item_labels = {
        i.key: f"{i.name.strip() or 'Без названия'} — {i.supplier} · "
               f"{('арт. ' + i.article + ' · ') if i.article else ''}код {i.sku}"
        for i in dataset.items
    }
    selected = st.selectbox(
        "Товар для уточнения", sorted(item_labels, key=lambda key: item_labels[key].casefold()),
        index=None, format_func=item_labels.__getitem__,
        placeholder="Выберите товар или введите название",
    )
    if selected is not None and not dataset.synthetic:
        selected_item = next(i for i in dataset.items if i.key == selected)
        historical = [m for m in selected_item.months if m.month <= as_of and m.opening_stock is not None]
        if historical:
            latest = max(historical, key=lambda m: m.month)
            st.info(f"Исторический остаток на начало {latest.month:%d.%m.%Y}: {latest.opening_stock:g} "
                    f"{selected_item.unit or '(единица не указана)'}. "
                    "Это не подтверждённый текущий свободный остаток; автоматически в заказ не подставляется.")
            if latest.stock_source:
                source = latest.stock_source
                st.caption(f"Источник: {source.file} / {source.sheet}!{source.cell}")
    new_stock = st.number_input("Свободный остаток на выбранную дату", min_value=0.0, value=None)
    month = st.date_input("Месяц подтверждённого stockout", value=as_of.replace(day=1))
    absent = st.number_input("Подтверждённые дни stockout", min_value=0, max_value=31, value=None)
    growth = st.number_input("Сценарный годовой прирост, %", min_value=-100.0, max_value=1000.0, value=None)
    if st.button("Применить уточнения", disabled=selected is None):
        if new_stock is not None:
            st.session_state.stocks[selected] = StockInput(quantity=new_stock, as_of=as_of)
        if absent is not None:
            st.session_state.stockouts.setdefault(selected, {})[month.replace(day=1)] = absent
        if growth is not None:
            st.session_state.growth[selected] = growth / 100
        reset_calculation()
    if st.button("Очистить уточнения"):
        st.session_state.stocks, st.session_state.stockouts, st.session_state.growth = {}, {}, {}
        reset_calculation()
    st.write("Уточнённых остатков:", len(st.session_state.stocks), "Товаров со stockout:", len(st.session_state.stockouts))
    if scenario_stockouts:
        st.caption(f"Отдельно применяются учебные периоды stockout: "
                   f"{sum(len(months) for months in scenario_stockouts.values())}. "
                   "Они сценарные, не подтверждённые данные партнёра.")

with st.expander("Метод и параметры обнаружения выбросов"):
    st.caption("Кандидат выше обоих порогов: медиана × множитель и медиана + множитель MAD. "
               "Несколько изолированных событий разных клиентов могут быть исключены. Повторные крупные клиенты "
               "и неоднозначные события без ID сохраняются. Нужны обычные продажи с обеих сторон всплеска "
               "и сверка месячных итогов. Это проверяемая эвристика, не норматив поставщика.")
    multiplier = st.number_input("Множитель медианы", min_value=2.0, max_value=20.0, value=4.0)
    mad = st.number_input("Множитель MAD", min_value=2.0, max_value=20.0, value=6.0)
    lookback = st.number_input("История для анализа, месяцев", min_value=3, max_value=36, value=12)

policy = None
try:
    if horizon is not None and lead is not None:
        stockout_days = {key: dict(months) for key, months in scenario_stockouts.items()}
        for key, months in st.session_state.stockouts.items():
            stockout_days.setdefault(key, {}).update(months)
        policy = Policy(as_of=as_of, horizon_days=horizon, lead_days=lead, suppliers=suppliers, categories=categories,
                        outlier_multiplier=multiplier, mad_multiplier=mad, lookback_months=lookback,
                        stocks=st.session_state.stocks, stockout_days=stockout_days,
                        growth_overrides=st.session_state.growth, category_horizons=category_horizons)
except ValidationError:
    st.error("Проверьте параметры: число дней stockout должно соответствовать выбранному месяцу.")
if horizon is None or lead is None:
    st.info("Для расчёта введите период между закупками и срок новой поставки. Поставщики и категории — необязательные фильтры.")
previous = st.session_state.get("calculation")
if previous and (policy is None or previous.policy != policy):
    reset_calculation()
if st.button("Рассчитать рекомендации", type="primary", disabled=policy is None):
    reset_agent()
    try:
        st.session_state.calculation = calculate_orders(dataset, policy)
        st.session_state.approval = None
    except Exception as exc:
        log.error("Calculation failure: %s", type(exc).__name__)
        reset_calculation()
        st.error("Расчёт не завершён. Проверьте входные данные; результат не сохранён.")

with st.expander("Проверка прогноза на прошлых месяцах"):
    st.caption("Отдельная проверка прогноза продаж, не исторического заказа и не экономии. "
               "Берём 3 завершённых месяца; при каждом прогнозе доступны только предшествующие продажи. "
               "Текущие коэффициенты из файлов не используются: сезонность оценивается по 24 прошлым месяцам, "
               "иначе явно принимается 1. Сравнение — с дневным темпом последнего месяца. "
               "Даты исправлений исходных файлов неизвестны; это не архив данных на каждую прошлую дату.")
    check_key = st.selectbox("Товар для проверки прогноза", list(item_labels),
                             format_func=item_labels.__getitem__, key="backtest_item")
    check_identity = (dataset_version, check_key, as_of.isoformat())
    if st.session_state.get("backtest_identity") != check_identity:
        st.session_state.pop("backtest_report", None)
        st.session_state.backtest_identity = check_identity
    if st.button("Проверить прогноз", key="backtest_run"):
        item = next(i for i in dataset.items if i.key == check_key)
        try:
            st.session_state.backtest_report = backtest_dataset(
                Dataset(items=[item], synthetic=dataset.synthetic), months=3, as_of=as_of)
        except Exception as exc:
            st.session_state.pop("backtest_report", None)
            log.error("Backtest failure: %s", type(exc).__name__)
            st.error("Проверка прогноза не завершена. Проверьте историю продаж.")
    if report := st.session_state.get("backtest_report"):
        evaluated = report["items"][0]
        scores = evaluated["metrics"]
        st.caption("Учебные продажи" if report["metadata"]["synthetic"] else "Продажи из файлов партнёра")
        if evaluated["evaluated_periods"]:
            st.write(f"Проверено месяцев: {evaluated['evaluated_periods']}. "
                     f"Средняя абсолютная ошибка (MAE): {scores['mae']:.2f} {evaluated['unit']}; "
                     f"простое сравнение: {scores['baseline_mae']:.2f} {evaluated['unit']}.")
            if scores["wape"] is not None:
                st.write(f"WAPE: {scores['wape']:.1%}; простое сравнение: {scores['baseline_wape']:.1%}.")
            else:
                st.info("WAPE не определена: сумма фактических продаж равна нулю.")
        else:
            st.warning("Недостаточно корректной истории для проверки прогноза.")
        reasons = {
            "invalid_item": "Ошибка данных товара", "missing_unit": "Неизвестна единица",
            "invalid_or_duplicate_month": "Некорректный или повторный месяц",
            "missing_target_sales": "Нет продаж проверяемого месяца",
            "invalid_target_sales": "Некорректные продажи проверяемого месяца",
            "missing_recent_training_month": "Нет трёх предыдущих месяцев",
            "invalid_training_sales": "Некорректная история продаж",
            "invalid_training_transaction": "Некорректная операция",
            "forecast_unavailable": "Прогноз недоступен", "nonfinite_forecast": "Некорректный прогноз",
        }
        periods = [{"Месяц": p["month"], "Продажи": p.get("actual_sales"),
                    "Прогноз": p.get("forecast"), "Простое сравнение": p.get("baseline_forecast"),
                    "Абсолютная ошибка": p.get("absolute_error"),
                    "Результат": "Проверен" if p["status"] == "ok" else reasons.get(p.get("reason"), "Недостаточно данных")}
                   for p in evaluated["periods"]]
        st.dataframe(pd.DataFrame(periods), hide_index=True,
                     column_config={name: st.column_config.NumberColumn(format="%.2f") for name in
                                    ("Продажи", "Прогноз", "Простое сравнение", "Абсолютная ошибка")})
        st.caption("Цель сравнения — наблюдаемые продажи, включая разовые покупки. "
                   "Упущенный спрос при stockout неизвестен. Большая ошибка не скрывается; "
                   "эти числа не доказывают точность регулярной закупки или снижение затрат.")

calculation = st.session_state.get("calculation")
if calculation is None:
    st.stop()
ok = [r for r in calculation.rows if r.status == "ok"]
blocked = [r for r in calculation.rows if r.status != "ok"]
needs_data = [r for r in blocked if r.status == "needs_data"]
invalid_data = [r for r in blocked if r.status == "invalid_data"]
to_order = [r for r in ok if r.quantity > 0]
with overview.container():
    st.caption("SYNTHETIC/TRAINING — результаты учебного примера" if calculation.synthetic
               else "Результаты по загруженным файлам партнёра")
    a, b, c = st.columns(3)
    a.metric("Рассчитано позиций", len(ok))
    b.metric("Заблокировано всего", len(blocked))
    c.metric("Позиций к закупке", len(to_order))
    st.caption(f"Среди заблокированных: нужны данные — {len(needs_data)}, ошибки данных — {len(invalid_data)}. "
               "Количество к закупке показано до ручных корректировок.")
urgent = [r for r in to_order if r.urgency.lower().startswith("срочно") or r.urgency.lower().startswith("дефицит")]
if urgent:
    st.warning("Срочно проверить: " + ", ".join(f"{r.supplier} · {r.sku}" for r in urgent))
elif to_order:
    st.caption("Срочных позиций по расчёту нет. Срочность определяется датой ожидаемого дефицита.")
if blocked:
    with st.expander(f"Заблокированные позиции: {len(blocked)}", expanded=not to_order):
        st.caption(
            f"{len(needs_data)} строк ожидают недостающие данные; {len(invalid_data)} содержат ошибки. "
            "Заблокированные строки не участвуют в числовом заказе."
        )
        st.dataframe(pd.DataFrame([{"Поставщик": r.supplier, "Код 1С": r.sku, "Артикул": r.article,
                                    "Товар": r.name, "Статус": r.status, "Почему заблокировано": r.explanation,
                                    "Предупреждения": "; ".join(r.warnings)} for r in blocked]), hide_index=True)
        if blocked:
            blocked_keys = [r.key for r in blocked]
            detail_key = st.selectbox(
                "Подробнее о заблокированной позиции", blocked_keys,
                format_func=lambda key: next(
                    f"{r.supplier} · {r.sku} — {r.name}" for r in blocked if r.key == key
                ),
            )
            detail = next(r for r in blocked if r.key == detail_key)
            st.markdown(f"**{detail.status}: {detail.supplier} · {detail.sku}**")
            st.write(detail.explanation or "Источник сообщил об ошибке без дополнительного описания.")
            if detail.warnings:
                for warning in detail.warnings:
                    st.write("• " + warning)
            if detail.evidence:
                st.caption("Источники позиции")
                st.dataframe(pd.DataFrame([s.model_dump() for s in detail.evidence]), hide_index=True)
show_agent(dataset, calculation, dataset_version)
if not ok:
    st.warning("Нет позиций, рассчитанных для заказа. Сначала устраните показанные причины блокировки.")
    st.stop()

st.subheader("Проект заказа по поставщикам")
st.caption("Откройте поставщика, проверьте состав и количество. Ноль исключает позицию; любое изменение требует причины.")
version = hashlib.sha256(calculation.model_dump_json().encode()).hexdigest()[:16]
order_suppliers = sorted({r.supplier for r in to_order})
supplier_tabs = st.tabs(order_suppliers) if order_suppliers else []
if not to_order:
    st.info("Пополнение не требуется: запаса достаточно по всем рассчитанным позициям.")
edited_by_supplier = {}
for supplier, supplier_tab in zip(order_suppliers, supplier_tabs):
    supplier_rows = [r for r in to_order if r.supplier == supplier]
    frame = pd.DataFrame([{"key": r.key, "Артикул": r.article, "Товар": r.name,
                           "Единица": r.unit, "Рекомендовано": r.quantity, "К заказу": r.quantity,
                           "Срочность": r.urgency} for r in supplier_rows])
    with supplier_tab:
        st.caption(f"Позиций к заказу: {len(supplier_rows)} · Всего рассчитано: {sum(r.supplier == supplier for r in ok)}")
        edited_by_supplier[supplier] = st.data_editor(
            frame, hide_index=True, disabled=[c for c in frame.columns if c != "К заказу"],
            key="draft_positive_" + version + "_" + hashlib.sha256(supplier.encode()).hexdigest()[:8],
            column_config={"key": None, "К заказу": st.column_config.NumberColumn(min_value=0.0)},
        )
reason = st.text_input("Причина корректировки (если меняли количество)", key="reason_" + version)
key = st.selectbox(
    "Показать расчёт позиции", [r.key for r in ok],
    format_func=lambda key: next(f"{r.supplier} · {r.sku} — {r.name}" for r in ok if r.key == key),
)
row = next(r for r in ok if r.key == key)
st.markdown(f"**{row.supplier} · {row.sku} — {row.name}**")
st.write(row.explanation)
with st.expander("Слагаемые, история и источники"):
    st.json(row.components)
    history = pd.DataFrame(row.history)
    if not history.empty:
        st.line_chart(history.set_index("month")[["sales", "cleaned"]])
        st.dataframe(history, hide_index=True)
    for warning in row.warnings:
        st.write("• " + warning)
    st.dataframe(pd.DataFrame([s.model_dump() for s in row.evidence]), hide_index=True)

try:
    quantities = {r.key: float(r.quantity or 0) for r in ok}
    for edited in edited_by_supplier.values():
        quantities.update({str(r["key"]): float(r["К заказу"]) for r in edited.to_dict("records")})
    current = fingerprint(calculation, quantities, reason)
    changed = any(quantities[r.key] != r.quantity for r in ok)
    valid_draft = any(q > 0 for q in quantities.values()) and (not changed or bool(reason.strip()))
    if st.session_state.get("approval") != current:
        st.session_state.approval = None
    if changed and not reason.strip():
        st.warning("Укажите причину корректировки перед утверждением.")
    a, b = st.columns(2)
    if a.button("Утвердить текущий заказ", disabled=not valid_draft):
        st.session_state.approval = current
    if b.button("Отклонить / снять утверждение"):
        st.session_state.approval = None
    if st.session_state.get("approval") == current:
        st.success("Текущая версия утверждена. Заказ поставщику не отправлялся.")
        st.download_button("Скачать утверждённый CSV", export_csv(calculation, quantities, current, reason),
                           file_name="synthetic_order.csv" if calculation.synthetic else "purchase_order.csv", mime="text/csv")
    else:
        st.info("Проект ожидает проверки и утверждения менеджером.")
except (ValueError, TypeError):
    st.session_state.approval = None
    st.error("Проверьте количество: конечное неотрицательное число, с соблюдением минимальной партии и кратности.")
