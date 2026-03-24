"""
Canonical project names for CropSight.

Used by the extraction prompt to normalize topic labels across meetings.
When a meeting discusses "Moldova PoC", "Gagauzia project", or "Moldova wheat",
the extraction should normalize all of these to "Moldova Pilot".

Update this list as CropSight's project portfolio evolves.
"""

CANONICAL_PROJECT_NAMES = [
    "Moldova Pilot",
    "Pre-Seed Fundraising",
    "SatYield Accuracy Model",
    "Product V1",
    "Business Plan",
    "EU Grant",
    "Website & Marketing",
    "Investor Outreach",
    "Operational Tooling",
    "Team & HR",
]


def get_canonical_names_for_prompt() -> str:
    """Format canonical names for inclusion in extraction prompt."""
    return ", ".join(f'"{name}"' for name in CANONICAL_PROJECT_NAMES)
