"""geoscan — only standards-declared, unambiguous hotel coordinates."""
from yta.geoscan import find_latlng

RISHIKESH = (30.130767, 78.328593)


def test_jsonld_geo_hotel_node():
    jl = [{"@type": "Hotel", "geo": {"latitude": 30.130767, "longitude": 78.328593}}]
    r = find_latlng("", jl, [])
    assert r[:2] == RISHIKESH and "JSON-LD" in r[2]


def test_jsonld_non_hotel_type_ignored():
    jl = [{"@type": "ImageObject", "geo": {"latitude": 1.0, "longitude": 2.0}}]
    assert find_latlng("", jl, []) is None


def test_jsonld_multiple_hotel_nodes_ambiguous():
    jl = [{"@type": "Hotel", "geo": {"latitude": 30.13, "longitude": 78.32}},
          {"@type": "Hotel", "geo": {"latitude": 28.61, "longitude": 77.20}}]
    assert find_latlng("", jl, []) is None


def test_og_meta():
    html = ('<meta property="og:latitude" content="30.130767" />'
            '<meta property="og:longitude" content="78.328593" />')
    r = find_latlng(html)
    assert r[:2] == RISHIKESH and "meta" in r[2]


def test_place_location_meta():
    html = ('<meta property="place:location:latitude" content="30.130767">'
            '<meta property="place:location:longitude" content="78.328593">')
    assert find_latlng(html)[:2] == RISHIKESH


def test_geo_position_meta():
    html = '<meta name="geo.position" content="30.130767; 78.328593">'
    assert find_latlng(html)[:2] == RISHIKESH


def test_microdata_geo():
    html = ('<span itemprop="geo" itemscope>'
            '<meta itemprop="latitude" content="30.130767">'
            '<meta itemprop="longitude" content="78.328593"></span>')
    assert find_latlng(html)[:2] == RISHIKESH


def test_single_data_attr_pair():
    html = '<div id="map" data-latitude="30.130767" data-longitude="78.328593"></div>'
    assert find_latlng(html)[:2] == RISHIKESH


def test_multiple_data_attrs_ambiguous():
    html = ('<div data-lat="30.13" data-lng="78.32"></div>'
            '<div data-lat="28.61" data-lng="77.20"></div>')
    assert find_latlng(html) is None


def test_bare_json_literals_not_used():
    # no standard declaration -> we do NOT guess from raw "latitude":x literals
    html = '{"latitude":30.130767,"longitude":78.328593,"name":"whatever"}'
    assert find_latlng(html) is None


def test_rejects_null_island_and_out_of_range():
    jl = [{"@type": "Hotel", "geo": {"latitude": 0, "longitude": 0}}]
    assert find_latlng("", jl) is None
    jl = [{"@type": "Hotel", "geo": {"latitude": 200, "longitude": 78.3}}]
    assert find_latlng("", jl) is None
