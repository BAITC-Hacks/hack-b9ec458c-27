from __future__ import annotations

import hashlib
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from replenishment.demo import demo_dataset
from replenishment.engine import calculate_orders
from replenishment.importer import load_dataset
from replenishment.models import Policy, StockInput
from replenishment.review import export_csv, fingerprint

log = logging.getLogger("replenishment.ui")
st.set_page_config(page_title="Пополнение склада", layout="wide")
st.title("Заказ без разовых всплесков")
st.caption("Регулярный спрос → доступный запас → обоснованное количество для закупки")


def reset_calculation():
    st.session_state.pop("calculation", None)
    st.session_state.pop("approval", None)


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
a, b, c = st.columns(3)
a.metric("Рассчитано позиций", len(ok))
b.metric("Нужна проверка данных", len(blocked))
c.metric("Предложено к закупке", sum(r.quantity > 0 for r in ok))
if blocked:
    with st.expander("Почему часть позиций не рассчитана", expanded=not ok):
        st.dataframe(pd.DataFrame([{"Поставщик": r.supplier, "Код": r.sku, "Статус": r.status,
                                    "Причина": r.explanation, "Подробности": "; ".join(r.warnings)} for r in blocked]), hide_index=True)
if not ok:
    st.warning("Нет рассчитанных позиций для утверждения.")
    st.stop()

st.subheader("Проект заказа по поставщикам")
st.caption("Измените количество при необходимости. Ноль исключает позицию из заказа.")
frame = pd.DataFrame([{"key": r.key, "Поставщик": r.supplier, "Артикул": r.article, "Товар": r.name,
                       "Единица": r.unit, "Рекомендовано": r.quantity, "К заказу": r.quantity,
                       "Срочность": r.urgency} for r in ok])
version = hashlib.sha256(calculation.model_dump_json().encode()).hexdigest()[:16]
edited = st.data_editor(frame, hide_index=True, disabled=[c for c in frame.columns if c != "К заказу"],
                        key="draft_" + version, column_config={"key": None,
                        "К заказу": st.column_config.NumberColumn(min_value=0.0)})
reason = st.text_input("Причина корректировки (если меняли количество)", key="reason_" + version)
key = st.selectbox("Показать расчёт позиции", [r.key for r in ok])
row = next(r for r in ok if r.key == key)
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
    quantities = {str(r["key"]): float(r["К заказу"]) for r in edited.to_dict("records")}
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
