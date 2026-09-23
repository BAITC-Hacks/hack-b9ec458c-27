from pathlib import Path

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
