import pytest

from ghl_attribution.mapping import (
    AttributionMapping,
    AttributionMappingError,
    CustomFieldDef,
)


def _defs(**overrides):
    specs = {
        "contact.attribution_source": ("cf_source", "TEXT"),
        "contact.attribution_medium": ("cf_medium", "TEXT"),
        "contact.attribution_campaign": ("cf_campaign", "TEXT"),
        "contact.attribution_content": ("cf_content", "TEXT"),
        "contact.attribution_landing_page": ("cf_lp", "TEXT"),
        "contact.attribution_first_touch_at": ("cf_first", "DATE"),
        "contact.attribution_last_touch_at": ("cf_last", "DATE"),
        "contact.gclid": ("cf_gclid", "TEXT"),
        "contact.fbclid": ("cf_fbclid", "TEXT"),
    }
    specs.update(overrides)
    fields = []
    for key, value in specs.items():
        if value is None:
            continue
        field_id, data_type = value
        fields.append(CustomFieldDef(id=field_id, field_key=key, data_type=data_type))
    return fields


def test_default_mapping_resolves_to_field_ids():
    resolved = AttributionMapping.default().resolve(_defs(), location_id="loc_test")

    assert resolved.location_id == "loc_test"
    assert resolved.field_id("utm_source") == "cf_source"
    assert resolved.field_id("last_touch_at") == "cf_last"
    assert resolved.ghl_key("utm_source") == "contact.attribution_source"


def test_missing_required_field_refuses_to_start():
    with pytest.raises(AttributionMappingError) as excinfo:
        AttributionMapping.default().resolve(_defs(**{"contact.attribution_campaign": None}))

    drift = excinfo.value.drift
    assert len(drift) == 1
    assert drift[0].key == "utm_campaign"
    assert drift[0].kind == "missing"


def test_data_type_drift_is_reported_not_coerced():
    with pytest.raises(AttributionMappingError) as excinfo:
        AttributionMapping.default().resolve(_defs(**{"contact.attribution_first_touch_at": ("cf_first", "TEXT")}))

    drift = excinfo.value.drift
    assert [item.kind for item in drift] == ["type"]
    assert "DATE" in drift[0].detail and "TEXT" in drift[0].detail


def test_duplicate_field_key_is_ambiguous():
    definitions = _defs()
    definitions.append(CustomFieldDef(id="cf_other", field_key="contact.attribution_source", data_type="TEXT"))

    with pytest.raises(AttributionMappingError) as excinfo:
        AttributionMapping.default().resolve(definitions)

    assert excinfo.value.drift[0].kind == "ambiguous"


def test_optional_field_may_be_absent():
    resolved = AttributionMapping.default().resolve(_defs(**{"contact.gclid": None}))

    assert "gclid" not in resolved.keys()
    assert "utm_source" in resolved.keys()


def test_all_problems_reported_at_once():
    with pytest.raises(AttributionMappingError) as excinfo:
        AttributionMapping.default().resolve(
            _defs(
                **{
                    "contact.attribution_campaign": None,
                    "contact.attribution_medium": ("cf_medium", "NUMERICAL"),
                }
            )
        )

    kinds = sorted(item.kind for item in excinfo.value.drift)
    assert kinds == ["missing", "type"]


def test_resolution_is_case_insensitive_on_keys():
    definitions = [
        CustomFieldDef(id=item.id, field_key=item.field_key.upper(), data_type=item.data_type)
        for item in _defs()
    ]

    resolved = AttributionMapping.default().resolve(definitions)
    assert resolved.field_id("utm_source") == "cf_source"
