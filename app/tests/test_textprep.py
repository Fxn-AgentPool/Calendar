from calapp.ingest.textprep import clean


def test_html_email_becomes_plain_text():
    text = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Dentist on <b>Tuesday</b> at 14:00</p><div>Praxis&nbsp;Weber</div>"
        "<script>alert(1)</script></body></html>"
    )
    assert clean(text, 1000) == "Dentist on Tuesday at 14:00\n\nPraxis Weber"


def test_whitespace_and_invisible_characters():
    text = "Lunch​  at\t\t12:30\r\n\r\n\r\n\r\nwith  Anna\x00 "
    assert clean(text, 1000) == "Lunch at 12:30\n\nwith Anna"


def test_quoted_replies_are_kept():
    text = "Sounds good!\n\n> Can we meet Tuesday at 3?"
    assert "Tuesday at 3" in clean(text, 1000)


def test_long_text_is_cut_at_the_end():
    result = clean("a" * 50, 20)
    assert result.startswith("a" * 20)
    assert result.endswith("[…cut]")


def test_plain_text_with_angle_brackets_is_not_treated_as_html():
    assert clean("x < 3 and y > 2", 100) == "x < 3 and y > 2"
