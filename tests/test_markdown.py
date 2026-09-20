from health_sync.writers.markdown import render_table


def test_cell_pipes_and_newlines_do_not_break_the_table():
    text = render_table(
        ["Test", "Value", "Notes"],
        [{"Test": "A | B", "Value": "5", "Notes": "first\nsecond || third"}],
    )
    assert text == (
        "| Test | Value | Notes |\n"
        "|---|---|---|\n"
        "| A &#124; B | 5 | first<br>second &#124;&#124; third |"
    )
