from uns_demo.tree import render
from uns_demo.uns import Message


def test_renders_the_namespace_as_an_indented_tree():
    text = render(
        {
            "planta1/exterior/pt-104/presion": Message(6.71, unit="bar"),
            "planta1/exterior/pi-104/presion": Message(
                6.8, unit="bar", source="vision", extra={"confidence": 0.98}
            ),
            "planta1/exterior/xv-104/estado": Message("cerrada", source="robot-01"),
        }
    )
    lines = text.splitlines()
    assert lines[0] == "planta1/"
    assert lines[1] == "  exterior/"
    assert "pi-104/" in lines[2]
    assert "6.80 bar" in text and "vision, 98%" in text
    assert "cerrada" in text and "robot-01" in text
