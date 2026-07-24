"""
One-off: force re-enrichment for LocationCells whose cached amenities/roads
predate the mall/road patches. Run with:
    python manage.py shell < force_refresh.py

Adjust the filter below to target the specific property (by geohash, or
just grab the most recently touched cell if you're re-testing the same pin).
"""
from property_intel.models import LocationCell
from property_intel.google_client import enrich_location_cell

# --- pick the cell(s) to force-refresh ---
cells = LocationCell.objects.order_by("-last_refreshed_at")[:1]  # adjust as needed

for cell in cells:
    print(f"Force-refreshing {cell.geohash} ({cell.formatted_address})")
    # Null out the two fetched_at flags that gate re-enrichment for the
    # fields that actually changed (shopping + roads). This is what makes
    # needs_enrichment() (and has_complete_data) treat this cell as
    # incomplete again, instead of waiting 90 days for is_stale.
    cell.amenities_fetched_at = None
    cell.major_road_context_fetched_at = None
    cell.save(update_fields=["amenities_fetched_at", "major_road_context_fetched_at"])

    cell, failures = enrich_location_cell(cell)
    print("  nearby_shopping:", cell.nearby_shopping)
    print("  nearby_roads:", cell.nearby_roads)
    if failures:
        print("  step failures:", failures)
