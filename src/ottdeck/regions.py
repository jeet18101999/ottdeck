"""Country codes TMDB reports availability for, with readable names."""
from __future__ import annotations

# Not exhaustive - just enough to label whatever the catalogue actually holds.
COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BE": "Belgium", "BR": "Brazil", "CA": "Canada",
    "CH": "Switzerland", "CL": "Chile", "CO": "Colombia", "CZ": "Czechia",
    "DE": "Germany", "DK": "Denmark", "EE": "Estonia", "EG": "Egypt",
    "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GR": "Greece", "HK": "Hong Kong", "HU": "Hungary", "ID": "Indonesia",
    "IE": "Ireland", "IL": "Israel", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "MX": "Mexico", "MY": "Malaysia",
    "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand", "PH": "Philippines",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RU": "Russia",
    "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "TH": "Thailand",
    "TR": "Turkey", "TW": "Taiwan", "UA": "Ukraine", "US": "United States",
    "VN": "Vietnam", "ZA": "South Africa",
}


def country_label(code: str) -> str:
    name = COUNTRY_NAMES.get(code.upper())
    return f"{name} ({code.upper()})" if name else code.upper()
