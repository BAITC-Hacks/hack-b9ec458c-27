from pathlib import Path
from unittest.mock import Mock

import pytest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_demo_calculation_and_approve_reject():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert any("SYNTHETIC/TRAINING" in warning.value for warning in app.warning)
    assert "Для менеджера закупа" in app.markdown[0].value
    assert "не отправляет заказ поставщику" in app.markdown[0].value
    button(app, "Рассчитать рекомендации").click().run()
    assert not app.exception
    assert app.session_state["approval"] is None
    button(app, "Утвердить текущий заказ").click().run()
    assert not app.exception
    token = app.session_state["approval"]
    assert token
    app.run()
    assert app.session_state["approval"] == token
    button(app, "Отклонить / снять утверждение").click().run()
    assert app.session_state["approval"] is None


def test_readme_demo_scenario_twice():
    for _ in range(2):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
        button(app, "Рассчитать рекомендации").click().run()
        assert not app.exception
        assert not app.get("download_button")
        assert any("Показать расчёт позиции" in element.label for element in app.selectbox)
        button(app, "Утвердить текущий заказ").click().run()
        assert not app.exception
        assert app.get("download_button")
        assert app.session_state["approval"]


def test_partner_rows_missing_data_show_blocking_reasons(tmp_path):
    source = tmp_path / "Ежемесячные продажи ИЭК.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Номенклатура", "Номенклатура.Код", "янв. 2026", "февр. 2026", "март 2026"])
    sheet.append([None, None, "Количество", "Количество", "Количество"])
    sheet.append(["Тестовая позиция", "001_", 10, 12, 11])
    workbook.save(source)
    workbook.close()

    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio[0].set_value("Файлы партнёра").run()
    app.text_area[0].set_value(str(tmp_path)).run()
    button(app, "Загрузить источники").click().run()
    assert not app.exception
    next(n for n in app.number_input if n.label == "Период между закупками, дней").set_value(30).run()
    next(n for n in app.number_input if n.label == "Срок новой поставки, дней").set_value(7).run()
    button(app, "Рассчитать рекомендации").click().run()

    assert not app.exception
    assert any("Заблокировано: нужны данные" in metric.label for metric in app.metric)
    assert any("Заблокированные позиции" in expander.label for expander in app.expander)
    assert any("Нужны данные" in element.value and "актуальный свободный остаток" in element.value
               for element in app.markdown)
    assert all("К заказу" not in str(frame.value) for frame in app.dataframe)


def test_parameter_change_invalidates_approval():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    button(app, "Рассчитать рекомендации").click().run()
    button(app, "Утвердить текущий заказ").click().run()
    assert app.session_state["approval"]
    next(n for n in app.number_input if n.label == "Период между закупками, дней").set_value(60).run()
    assert "calculation" not in app.session_state
    assert "approval" not in app.session_state


def test_mode_change_clears_old_draft():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    button(app, "Рассчитать рекомендации").click().run()
    button(app, "Утвердить текущий заказ").click().run()
    app.radio[0].set_value("Файлы партнёра").run()
    assert not app.exception
    assert "approval" not in app.session_state
    assert "calculation" not in app.session_state


class FakeAgentModel:
    """Exercise the real dispatcher without a network or credentials."""
    def __init__(self):
        self.calls = 0
        self.closed = False

    def complete(self, messages, tools):
        from replenishment.agent import ModelReply, ToolCall
        self.calls += 1
        if self.calls == 1:
            calls = [ToolCall(id="calc", name="calculate_replenishment")]
            if any(tool["function"]["name"] == "prepare_order" for tool in tools):
                calls.append(ToolCall(id="prepare", name="prepare_order"))
            return ModelReply(calls=calls)
        # Intentionally false narrative must never authorize or set quantity.
        return ModelReply(text="Заказ утверждён: 999999 штук.")

    def close(self):
        self.closed = True


@pytest.fixture
def agent_factory(monkeypatch):
    models = []
    def create(**kwargs):
        model = FakeAgentModel()
        models.append(model)
        return model
    factory = Mock(side_effect=create)
    factory.models = models
    monkeypatch.setattr("replenishment.agent_client.model_from_env", factory)
    return factory


def agent_app(request_order=True):
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    button(app, "Рассчитать рекомендации").click().run()
    app.text_input(key="agent_question").set_value("Почему столько к заказу?").run()
    app.checkbox(key="agent_request_order").set_value(request_order).run()
    return app


@pytest.mark.parametrize("request_order", [False, True])
def test_agent_submit_only_and_structured_result(agent_factory, request_order, monkeypatch):
    from replenishment.review import export_csv
    export = Mock(wraps=export_csv)
    monkeypatch.setattr("replenishment.review.export_csv", export)
    app = agent_app(request_order)
    assert agent_factory.call_count == 0
    app.button(key="agent_submit").click().run()
    assert not app.exception
    result = app.session_state["agent_result"]
    assert result.status == ("pending_approval" if request_order else "ok")
    assert result.recommendation.quantity == 85
    assert bool(result.pending_order) == request_order
    assert "agent_approval" not in app.session_state
    assert not app.get("download_button")
    assert not export.called
    assert next(m.value for m in app.metric if m.label == "Проверенное количество по расчёту") == "85 шт"
    assert any("Текст модели может ошибаться" in c.value for c in app.caption)
    assert any("999999" in t.value for t in app.text)
    assert any(e.label == "AI-пояснение модели (непроверенный текст)" for e in app.expander)
    if request_order:
        assert any("AI-проект не утверждён" in w.value for w in app.warning)
    else:
        assert any("Подготовка AI-проекта заказа не запрошена" in i.value for i in app.info)
    app.run()
    # Viewing another local explanation is a rerun, not a new agent request.
    next(s for s in app.selectbox if s.label == "Показать расчёт позиции").select_index(1).run()
    assert not app.exception
    assert agent_factory.call_count == 1
    assert agent_factory.call_args.kwargs == {"complex_task": request_order}
    assert agent_factory.models[0].calls == 2
    assert agent_factory.models[0].closed
    assert "agent_approval" not in app.session_state


def test_enhanced_explanation_selects_complex_model_without_order(agent_factory):
    app = agent_app(request_order=False)
    app.checkbox(key="agent_deep_analysis").check().run()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert agent_factory.call_args.kwargs == {"complex_task": True}
    assert app.session_state["agent_result"].status == "ok"
    assert "agent_approval" not in app.session_state
    assert not app.get("download_button")


def test_agent_approve_reject_separate_scope(agent_factory, monkeypatch):
    from replenishment.review import export_csv, fingerprint
    export = Mock(wraps=export_csv)
    monkeypatch.setattr("replenishment.review.export_csv", export)
    app = agent_app()
    app.button(key="agent_submit").click().run()
    assert not export.called
    assert any("AI-проект не утверждён" in w.value for w in app.warning)
    assert not app.get("download_button")
    app.button(key="agent_approve").click().run()
    pending = app.session_state["agent_result"].pending_order
    assert app.session_state["agent_approval"] == fingerprint(pending.calculation, pending.quantities)
    assert app.session_state["approval"] is None
    assert len(export.call_args.args[0].rows) == 1
    assert set(export.call_args.args[1]) == {app.selectbox(key="agent_item").value}
    assert pending.calculation.synthetic
    assert any("AI-проект одной позиции утверждён" in s.value for s in app.success)
    assert not any("AI-проект не утверждён" in w.value for w in app.warning)
    assert any(d.label == "Скачать утверждённый AI-проект CSV" for d in app.get("download_button"))
    calls = export.call_count
    app.button(key="agent_reject").click().run()
    assert "agent_approval" not in app.session_state
    assert not app.get("download_button")
    assert any("AI-проект не утверждён" in w.value for w in app.warning)
    assert export.call_count == calls
    assert agent_factory.call_count == 1
    button(app, "Утвердить текущий заказ").click().run()
    assert app.session_state["approval"]
    assert "agent_approval" not in app.session_state


@pytest.mark.parametrize("change", ["question", "item", "checkbox", "deep_analysis", "policy", "data", "mode", "recalculate"])
def test_agent_inputs_invalidate_response_and_approval(agent_factory, monkeypatch, change):
    app = agent_app()
    app.button(key="agent_submit").click().run()
    app.button(key="agent_approve").click().run()
    assert app.session_state["agent_approval"]
    if change == "question":
        app.text_input(key="agent_question").set_value("Другой вопрос").run()
    elif change == "item":
        app.selectbox(key="agent_item").select_index(1).run()
    elif change == "checkbox":
        app.checkbox(key="agent_request_order").uncheck().run()
    elif change == "deep_analysis":
        app.checkbox(key="agent_deep_analysis").check().run()
    elif change == "policy":
        next(n for n in app.number_input if n.label == "Период между закупками, дней").set_value(60).run()
    elif change == "data":
        from replenishment.demo import demo_dataset
        changed = demo_dataset()
        changed.items[0].stock += 1
        monkeypatch.setattr("replenishment.demo.demo_dataset", lambda: changed)
        app.run()
    elif change == "mode":
        app.radio[0].set_value("Файлы партнёра").run()
    else:
        button(app, "Рассчитать рекомендации").click().run()
    assert not app.exception
    assert "agent_result" not in app.session_state
    assert "agent_approval" not in app.session_state
    assert agent_factory.call_count == 1
    assert not any(d.label == "Скачать утверждённый AI-проект CSV" for d in app.get("download_button"))


@pytest.mark.parametrize("status", ["needs_data", "invalid_data", "not_found", "model_error",
                                   "tool_error", "model_unavailable", "data_not_authorized"])
def test_agent_error_status_never_offers_approval(agent_factory, monkeypatch, status):
    from replenishment.agent import AgentResult
    monkeypatch.setattr("replenishment.agent.run_agent", lambda *a, **kw: AgentResult(
        status=status, message="Проверьте данные или подключение.", narrative="HIDDEN_NARRATIVE", synthetic=True))
    app = agent_app()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert any(status in warning.value for warning in app.warning)
    assert not any(b.key == "agent_approve" for b in app.button)
    assert not app.get("download_button")
    assert not any("HIDDEN_NARRATIVE" in t.value for t in app.text)
    assert "agent_approval" not in app.session_state


def test_agent_no_configuration_keeps_local_order_working(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    app = agent_app()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert app.session_state["agent_result"].status == "model_unavailable"
    button(app, "Утвердить текущий заказ").click().run()
    assert app.session_state["approval"]


def test_agent_adapter_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr("replenishment.agent_client.model_from_env", Mock(side_effect=RuntimeError("PRIVATE_ERROR_DETAIL")))
    app = agent_app()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert app.session_state["agent_result"].status == "model_unavailable"
    assert "PRIVATE_ERROR_DETAIL" not in str(app)


def test_agent_partner_data_never_creates_api_client(agent_factory, monkeypatch, tmp_path):
    from replenishment.demo import demo_dataset
    partner = demo_dataset()
    partner.synthetic = False
    monkeypatch.setattr("replenishment.importer.load_dataset", lambda paths: partner)
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.radio[0].set_value("Файлы партнёра").run()
    app.text_area[0].set_value(str(tmp_path)).run()
    button(app, "Загрузить источники").click().run()
    next(n for n in app.number_input if n.label == "Период между закупками, дней").set_value(30).run()
    next(n for n in app.number_input if n.label == "Срок новой поставки, дней").set_value(7).run()
    button(app, "Рассчитать рекомендации").click().run()
    app.text_input(key="agent_question").set_value("Объясни расчёт").run()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert app.session_state["agent_result"].status == "data_not_authorized"
    assert not agent_factory.called
    assert "agent_approval" not in app.session_state


@pytest.mark.parametrize("quantity", [90, 1.5])
def test_agent_changed_project_cannot_reuse_approval(agent_factory, quantity):
    app = agent_app()
    app.button(key="agent_submit").click().run()
    app.button(key="agent_approve").click().run()
    pending = app.session_state["agent_result"].pending_order
    pending.quantities[next(iter(pending.quantities))] = quantity
    app.run()
    assert not app.exception
    assert "agent_approval" not in app.session_state
    assert not app.get("download_button")
    if quantity == 1.5:
        assert not any(b.key == "agent_approve" for b in app.button)
    assert agent_factory.call_count == 1


@pytest.mark.parametrize("failure", ["model_error", "tool_error"])
def test_agent_dispatch_failure_closes_client_without_leaking(monkeypatch, failure):
    from replenishment.agent import ModelReply, ToolCall
    model = FakeAgentModel()
    if failure == "model_error":
        model.complete = Mock(side_effect=RuntimeError("PRIVATE_ERROR_DETAIL"))
    else:
        model.complete = Mock(return_value=ModelReply(calls=[ToolCall(id="bad", name="unsupported_tool")]))
    monkeypatch.setattr("replenishment.agent_client.model_from_env", lambda **kwargs: model)
    app = agent_app()
    app.button(key="agent_submit").click().run()
    assert not app.exception
    assert model.closed
    assert app.session_state["agent_result"].status == failure
    assert "PRIVATE_ERROR_DETAIL" not in str(app)
    assert not any(b.key == "agent_approve" for b in app.button)
    assert not app.get("download_button")
