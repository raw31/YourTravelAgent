"""Generic occupancy resolution — URL signal shapes + reconciler."""
from yta.occupancy import signals_from_url, resolve, even_split


def _occ(url, **kw):
    return resolve(signals_from_url(url), **kw)[0]


# -- URL shape recognition (brand-agnostic) --------------------------

def test_booking_per_room_letters():
    occ = _occ("https://secure.booking.com/book.html?room1=A%2CA%2C7&room2=A%2CA")
    assert occ == [{"adults": 2, "children": 1, "child_ages": [7]},
                   {"adults": 2, "children": 0, "child_ages": []}]


def test_expedia_per_room_codes():
    occ = _occ("https://www.expedia.co.in/h1.Hotel?rm1=a2:c5:c3&rm2=a1")
    assert occ == [{"adults": 2, "children": 2, "child_ages": [5, 3]},
                   {"adults": 1, "children": 0, "child_ages": []}]


def test_mmt_flat_stream_beats_rsc_aggregate():
    occ = _occ("https://www.makemytrip.com/hotels/hotel-review/"
               "?roomStayQualifier=2e1e3e2e1e2e&rsc=2e4e2e3e2")
    assert occ == [{"adults": 2, "children": 1, "child_ages": [3]},
                   {"adults": 2, "children": 1, "child_ages": [2]}]


def test_agoda_token_plus_room_count():
    occ = _occ("https://www.agoda.com/en-in/book/?roomToken=8b%3A0%3Bo%3A2%3B"
               "p%3A0%3Bmo%3A2%3Bsai%3A20987%3Brcy%3AINR&nr0=2")
    assert occ == [{"adults": 2, "children": 0, "child_ages": []},
                   {"adults": 2, "children": 0, "child_ages": []}]


def test_generic_aggregate_params_any_ota():
    occ, conf, src, notes = resolve(signals_from_url(
        "https://any-ota.example/checkout?adults=4&children=2&rooms=2&age=6&age=9"))
    assert occ == [{"adults": 2, "children": 1, "child_ages": [6]},
                   {"adults": 2, "children": 1, "child_ages": [9]}]
    assert src == "even_split" and conf < 0.7
    assert any("assumed an even distribution" in n for n in notes)


def test_packed_guest_string():
    occ = _occ("https://x.example/book?guests=2a1c")
    assert occ == [{"adults": 2, "children": 1, "child_ages": [12]}]


# -- reconciler --------------------------------------------------------

def test_even_split_remainder_to_first_rooms():
    assert even_split(2, 4, 3) == [
        {"adults": 2, "children": 2, "child_ages": [12, 12]},
        {"adults": 2, "children": 1, "child_ages": [12]}]
    assert even_split(3, 5, 0) == [
        {"adults": 2, "children": 0, "child_ages": []},
        {"adults": 2, "children": 0, "child_ages": []},
        {"adults": 1, "children": 0, "child_ages": []}]


def test_llm_per_room_read_is_a_signal():
    occ, conf, src, _ = resolve(
        [], llm_occupancy=[{"adults": 2, "children": 1, "child_ages": [4]}])
    assert occ == [{"adults": 2, "children": 1, "child_ages": [4]}]
    assert src == "llm:per_room"


def test_url_per_room_beats_llm_aggregate():
    occ, conf, src, _ = resolve(
        signals_from_url("https://booking.com/book.html?room1=A%2CA&room2=A"),
        llm_rooms=2, llm_adults=3)          # LLM only had the aggregate
    assert occ == [{"adults": 2, "children": 0, "child_ages": []},
                   {"adults": 1, "children": 0, "child_ages": []}]
    assert src == "url:roomN"


def test_single_room_aggregate_is_not_flagged():
    occ, conf, src, notes = resolve(signals_from_url(
        "https://x.example/book?adults=2&rooms=1"))
    assert occ == [{"adults": 2, "children": 0, "child_ages": []}]
    assert conf >= 0.7 and not any("even distribution" in n for n in notes)


def test_unresolvable_returns_empty():
    occ, conf, src, notes = resolve(signals_from_url("https://x.example/book?foo=bar"))
    assert occ == [] and conf == 0.0


def test_guests_only_no_split_fails():
    # "6 guests" with no adult/child breakdown -> cannot build occupancy
    occ, _, src, _ = resolve(signals_from_url("https://x.example/book?rooms=2"))
    assert occ == [] and src == "rooms_only"
