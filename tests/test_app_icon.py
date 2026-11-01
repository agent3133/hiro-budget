from hiro_budget.gui.app_icon import SIZES, app_icon, drawing


def test_drawing_per_size():
    assert drawing(16).name == "app-16.svg"
    assert drawing(24).name == "app-32.svg"
    assert drawing(48).name == "app.svg"


def test_icon_has_every_size(qapp):
    icon = app_icon()
    assert [s.width() for s in icon.availableSizes()] == list(SIZES)
    assert not icon.pixmap(32).toImage().isNull()
