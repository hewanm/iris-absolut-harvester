import math

import pytest

from iris_harvester.classify import Taxonomy
from iris_harvester.config import Settings
from iris_harvester.geocode import Gazetteer, Place, coords_valid, to_25832


@pytest.fixture(scope="module")
def taxonomy():
    return Taxonomy.load(Settings().taxonomy_path)


@pytest.fixture(scope="module")
def gazetteer():
    s = Settings()
    return Gazetteer.load(s.gazetteer_path, s.institutions_path)


# --- classification ---------------------------------------------------------

def test_keeps_institutional_real_asset_signal(taxonomy):
    c = taxonomy.classify("Pensionskasse erhöht Immobilienquote",
                          "Spezialfonds-Mandat im Rahmen der Anlageverordnung", has_german_place=True)
    assert c.keep
    assert {"institutional_investors", "real_assets", "regulation_quotas"} <= set(c.topics)
    assert c.geo_scope == "DE"


def test_drops_off_topic_item(taxonomy):
    c = taxonomy.classify("Volatilität an den Aktienmärkten", "Analysten erwarten Schwankungen")
    assert not c.keep and c.reject_reason == "no_objective_topic"


def test_drops_swiss_only_item(taxonomy):
    c = taxonomy.classify("Schweizer Pensionskassen erhöhen Immobilienquote", "Die FINMA beobachtet")
    assert not c.keep and c.reject_reason == "out_of_scope_geo:CH"


def test_dach_item_with_german_context_is_kept(taxonomy):
    c = taxonomy.classify("Versicherer in Deutschland und der Schweiz bauen Infrastruktur aus", "")
    assert c.keep and c.geo_scope == "DACH"


def test_weak_single_mention_is_below_threshold(taxonomy):
    c = taxonomy.classify("Neues Portfolio-Tool", "Ein Anbieter erwähnt Immobilie am Rande")
    assert not c.keep and c.reject_reason == "below_min_score"


def test_unknown_geo_scope_is_kept_but_flagged(taxonomy):
    c = taxonomy.classify("Versicherer erhöhen Private-Debt-Quote", "Neue Mandate für Infrastruktur")
    assert c.keep and c.geo_scope == "UNKNOWN" and "geo_scope_unknown" in c.flags


def test_taxonomy_rejects_unknown_required_topic():
    with pytest.raises(ValueError):
        Taxonomy({"topics": {"a": {"patterns": ["x"]}}, "keep_rule": {"required_any": ["nope"]}})


# --- geocoding --------------------------------------------------------------

def test_resolves_single_city_to_epsg25832(gazetteer):
    g = gazetteer.resolve("Pensionskasse in Köln erhöht Immobilienquote")
    assert g.status == "resolved" and g.location_text == "Köln" and g.method == "place_name"
    # independent check of the projection (Cologne ~ E 356.7 km, N 5,644.9 km in UTM 32N)
    assert g.x == pytest.approx(356_689, abs=5) and g.y == pytest.approx(5_644_856, abs=5)


def test_adjective_form_resolves_with_assumption_flag(gazetteer):
    g = gazetteer.resolve("Frankfurter Zusatzversorgungskasse schichtet um")
    assert g.status == "resolved" and g.location_text == "Frankfurt am Main"
    assert "location_assumption" in g.flags and g.confidence < 0.9


def test_longest_match_wins_for_frankfurt_oder(gazetteer):
    g = gazetteer.resolve("Frankfurt (Oder): Pensionskasse prüft Windpark")
    assert g.status == "resolved" and g.location_text == "Frankfurt (Oder)"


def test_multiple_cities_are_ambiguous_without_point(gazetteer):
    g = gazetteer.resolve("Versorgungswerk aus Münster und Stiftung aus Hamburg")
    assert g.status == "ambiguous" and g.x is None
    assert {c["name"] for c in g.candidates} == {"Münster", "Hamburg"}


def test_state_only_mention_never_becomes_a_point(gazetteer):
    g = gazetteer.resolve("Bayerische Sparkassen bündeln Spezialfonds")
    assert g.status == "region_only" and g.location_text == "Bayern" and g.x is None


def test_institution_hq_masks_state_adjective(gazetteer):
    g = gazetteer.resolve("Die Bayerische Versorgungskammer erhöht die Infrastrukturquote")
    assert g.status == "resolved" and g.location_text == "München" and g.method == "institution_hq"


def test_acronym_institutions_require_exact_case(gazetteer):
    assert gazetteer.resolve("Die VBL vergibt ein Mandat").location_text == "Karlsruhe"
    assert gazetteer.resolve("vbl ist hier kein Kürzel").status == "not_found"


def test_city_and_its_own_institution_are_not_ambiguous(gazetteer):
    g = gazetteer.resolve("Talanx aus Hannover baut Infrastruktur aus")
    assert g.status == "resolved" and g.location_text == "Hannover"


def test_no_location(gazetteer):
    assert gazetteer.resolve("Versicherer prüfen Solvency II").status == "not_found"


@pytest.mark.parametrize("lon,lat", [(2.35, 48.86), (11.5, 95.0), (float("nan"), 50.0), (None, 50.0)])
def test_invalid_coordinates_are_rejected(lon, lat):
    assert not coords_valid(lon, lat)


def test_gazetteer_row_with_bad_coordinates_yields_invalid_status():
    gz = Gazetteer([Place("Musterstadt", "city", 48.86, 2.35, "XX", 0.9)], [])  # Paris coords
    g = gz.resolve("Pensionskasse aus Musterstadt")
    assert g.status == "invalid" and g.x is None and "invalid_coordinates" in g.flags


def test_projection_guard_rejects_points_outside_germany_extent():
    with pytest.raises(ValueError):
        to_25832(2.35, 48.86)
    x, y = to_25832(13.405, 52.52)  # Berlin lies east of zone 32 but inside the extended extent
    assert math.isfinite(x) and 700_000 < x < 900_000
