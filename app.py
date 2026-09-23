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
from replenishment.engine import calculate_orders
from replenishment.importer import load_dataset
from replenishment.models import Policy, StockInput
from replenishment.review import export_csv, fingerprint

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

if mode == "Учебный пример":
    st.warning("SYNTHETIC/TRAINING — вымышленные данные. Этот режим проверяет поведение, а не фактическую потребность компании.")
    dataset = demo_dataset()
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

with st.expander("Подтверждённые уточнения менеджера"):
    st.caption("Вводите только подтверждённые значения. Они заменяют соответствующий вход и сохраняются в текущей сессии.")
    selected = st.selectbox("Товар для уточнения", [i.key for i in dataset.items], index=None)
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

with st.expander("Метод и параметры обнаружения выбросов"):
    st.caption("Правило: одиночное событие выше обоих порогов — медиана × множитель и медиана + множитель MAD. Повторяющиеся крупные события сохраняются. Это открытая эвристика, не норматив поставщика.")
    multiplier = st.number_input("Множитель медианы", min_value=2.0, max_value=20.0, value=4.0)
    mad = st.number_input("Множитель MAD", min_value=2.0, max_value=20.0, value=6.0)
    lookback = st.number_input("История для анализа, месяцев", min_value=3, max_value=36, value=12)

policy = None
try:
    if horizon is not None and lead is not None:
        policy = Policy(as_of=as_of, horizon_days=horizon, lead_days=lead, suppliers=suppliers, categories=categories,
                        outlier_multiplier=multiplier, mad_multiplier=mad, lookback_months=lookback,
                        stocks=st.session_state.stocks, stockout_days=st.session_state.stockouts,
                        growth_overrides=st.session_state.growth)
except ValidationError:
    st.error("Проверьте параметры: число дней stockout должно соответствовать выбранному месяцу.")
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

calculation = st.session_state.get("calculation")
if calculation is None:
    st.stop()
ok = [r for r in calculation.rows if r.status == "ok"]
blocked = [r for r in calculation.rows if r.status != "ok"]
needs_data = [r for r in blocked if r.status == "needs_data"]
invalid_data = [r for r in blocked if r.status == "invalid_data"]
to_order = [r for r in ok if r.quantity > 0]
a, b, c = st.columns(3)
a.metric("Рассчитано позиций", len(ok))
b.metric("Заблокировано: нужны данные", len(needs_data))
c.metric("Позиций к закупке", len(to_order))
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
order_suppliers = sorted({r.supplier for r in ok})
supplier_tabs = st.tabs(order_suppliers)
edited_by_supplier = {}
for supplier, supplier_tab in zip(order_suppliers, supplier_tabs):
    supplier_rows = [r for r in ok if r.supplier == supplier]
    frame = pd.DataFrame([{"key": r.key, "Артикул": r.article, "Товар": r.name,
                           "Единица": r.unit, "Рекомендовано": r.quantity, "К заказу": r.quantity,
                           "Срочность": r.urgency} for r in supplier_rows])
    with supplier_tab:
        st.caption(f"Позиций к заказу: {sum(r.quantity > 0 for r in supplier_rows)} · Всего рассчитано: {len(supplier_rows)}")
        edited_by_supplier[supplier] = st.data_editor(
            frame, hide_index=True, disabled=[c for c in frame.columns if c != "К заказу"],
            key="draft_" + version + "_" + hashlib.sha256(supplier.encode()).hexdigest()[:8],
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
