# Агентный backend: контракт для DEV B

Backend включён в `main`, `app.py` вызывает агента по кнопке.
Используется ограниченный цикл function
calling; OpenAI Agents SDK не требуется. Асинхронного цикла и фоновых задач нет.
Расчётный движок и существующий механизм подтверждения сохранены.

## Сценарий и инструменты

Менеджер выбирает одну точную позицию `supplier:sku`, параметры и задаёт вопрос.
Backend проверяет входы и рассчитывает позицию. При неизвестном ключе, ошибке
источника, `needs_data` или `invalid_data` модель не вызывается.

| Tool | Результат | Побочные действия |
|---|---|---|
| `get_dataset_status` | Статус выбранной позиции, synthetic | Нет |
| `calculate_replenishment` | Проверенный расчёт, слагаемые, источники | Нет; использует результат preflight |
| `prepare_order` | Проект `pending_approval` | Нет экспорта, отправки или approval |

Аргументы всех tools пустые: модель не может изменить SKU, количество, параметры
или approval. `prepare_order` доступен только при `request_order=True` и положительном
количестве. Сначала модель должна прочитать расчёт. Неверный tool, аргументы или
повторный ID вызова прерывают запрос. Максимум 6 ответов модели и 3 tool calls в ответе.
HTTP timeout 20 секунд, автоматических retry нет. Это операционные лимиты, не бизнес-пороги.

## Публичный вызов

```python
from replenishment.agent import run_agent
from replenishment.agent_client import model_from_env

# dataset, policy, item_key принадлежат UI.
model = model_from_env(complex_task=request_order_checkbox or enhanced_explanation_checkbox)
try:
    result = run_agent(
        dataset, policy, item_key, question,
        request_order=request_order_checkbox,
        model=model,
        allow_partner_data=False,
    )
finally:
    if model is not None:
        model.close()
```

`AgentResult` содержит:

- `status`: `ok`, `pending_approval`, `needs_data`, `invalid_data`, `not_found`,
  `model_unavailable`, `model_error`, `tool_error`, `data_not_authorized`;
- `message`: короткий статус backend без сырых exception;
- `recommendation`: проверенная `Recommendation` либо None; ошибки модели сохраняют
  доступный локальный расчёт;
- `narrative`: текст модели, не источник количества, статуса или разрешения;
- `pending_order`: только при успешной подготовке; содержит `Calculation` выбранной
  позиции и `quantities`, не содержит approval;
- `tool_trace`: фактически выполненные имена tools, без сырых запросов;
- `synthetic`: маркировка учебных данных.

`model_error` не означает успешный AI-анализ. Без API обычный Streamlit/CLI продолжает
работать. Числа и состояния выводить из структурированных полей; narrative обозначать
как AI-пояснение. Полная точность свободного текста живой модели не доказана.

## Реализованная UI-интеграция и её правила

1. Вызывать агент только при нажатии кнопки AI/submit. Сохранять ответ в session_state,
   привязав к данным, параметрам, выбранному ключу, вопросу и checkbox. Изменение входа
   сбрасывает ответ и approval; обычный rerun не должен повторять запрос к API.
2. Показывать pending-проект отдельно с явным scope одной позиции. Не подменять им
   молча весь многопозиционный заказ текущего UI.
3. Сохранить `review.fingerprint` / `review.export_csv` и Approve/Reject. Только
   обработчик реального клика Approve записывает fingerprint текущего проекта.
   Изменение данных/количеств/причины требует нового подтверждения; Reject очищает его.
   Текст «да», narrative и `pending_approval` не означают approval.
4. Не передавать fingerprint модели. `export_csv` проверяет версию, но не личность
   человека: состояние approval должно принадлежать доверенному UI. Агент не имеет
   инструмента экспорта или отправки поставщику.
5. Ошибки настройки адаптера показывать коротко, без `str(exception)` и traceback.
6. Проверить checkbox off/on, rerun без API, Approve/Reject, изменение после Approve,
   ошибку API и недоступные данные. Эти сценарии проверены через AppTest с fake-моделью;
   вызов живой модели через UI ещё не проверен.

## Настройка и приватность

`python -m pip install -r requirements-agent.txt` устанавливает опциональный адаптер.
`requirements.txt` достаточно для ядра, UI и тестов с fake-моделью.

Переменные процесса: `AGENT_API_KEY`, опциональные `AGENT_MODEL_LIGHT`,
`AGENT_MODEL_COMPLEX`, `AGENT_MODEL` и `AGENT_BASE_URL`. Без переопределения простое
пояснение идёт в `gpt-6-luna`, усиленное пояснение и подготовка проекта — в `gpt-6-sol`. Старый
`AGENT_MODEL` переопределяет обе модели; отдельные настройки имеют приоритет.
Обе модели при function calling через Chat Completions используют
`reasoning_effort="none"`. Ключ не ищется в других проектах. Default endpoint:
`https://api.openai.com/v1`.
Допустим другой HTTPS Chat Completions endpoint; совместимость конкретной NVIDIA-модели
и параметров пока не проверена. `.env` автоматически не читается: настройте окружение
запуска IDE или терминала. Секреты не коммитить.

Реальный Dataset по умолчанию не отправляется модели. `allow_partner_data=True`
разрешает ответственный пользователь/конфигурация, а не текст модели. Наружу идут
вопрос и агрегаты выбранной позиции, объяснение, предупреждения, единица/категория,
имена файлов/листов/ячеек источников. Сырые накладные, customer_id, история транзакций
и абсолютные пути исключены. Вопрос и предупреждения не обезличиваются автоматически:
не помещать в них персональные данные или секреты.

Содержимое источников передаётся как данные. Тест с вредоносным текстом проверяет
диспетчер с fake-моделью, но не доказывает устойчивость живой LLM к prompt injection.
Протокол: [официальная документация function calling](https://developers.openai.com/api/docs/guides/function-calling).

## Сверка с ТЗ

| Must-have | Код / тесты | Ограничение |
|---|---|---|
| Влияние исходных данных | engine, importer; stock_and_inbound, supplied_growth, category_selection | Отсутствующий актуальный остаток IEK не восполнен |
| Сезонность и рост | seasonality_changes_future_forecast, sustained_growth_detected_separately | Проверены свойства, не точность реального прогноза |
| Stockout | confirmed_stockout_increases_estimate, full_stockout_uses_observed_reference | Точных длительностей нет в исходных файлах |
| Крупные разовые покупки | isolated_spike_does_not_inflate_order, split_customer_purchase_grouped | Customer ID только в синтетических тестах |
| Объяснимый заказ по поставщикам | engine, текущий UI, test_review | Импорт в конкретную 1С не проверен |

Имена тестов сокращены; полные функции — в `tests/test_engine.py`. Агент не закрывает
отсутствующие бизнес-данные. Полное выполнение всех Must-have на реальных данных не
заявляется. UI-интеграция проверена через AppTest с подменённой моделью. Следующий
шаг: выбранная живая модель на синтетическом примере. Встроенный табличный экспорт
скрыт настройкой `client.disableDataExport` в `.streamlit/config.toml`; явные кнопки
CSV приложения остаются привязаны к Approve/Reject.
