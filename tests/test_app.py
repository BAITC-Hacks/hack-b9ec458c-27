from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_demo_calculation_and_approve_reject():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
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
