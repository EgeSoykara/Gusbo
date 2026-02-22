NC_CITY_DISTRICT_MAP = {
    "Lefkosa": [
        "Hamitkoy",
        "Kumsal",
        "Ortakoy",
        "Gonyeli",
        "Metehan",
    ],
    "Girne": [
        "Karakum",
        "Ozankoy",
        "Catalkoy",
        "Alsancak",
        "Lapta",
    ],
    "Gazimagusa": [
        "Surlarici",
        "Tuzla",
        "Karakol",
        "Dogu Akdeniz",
        "Maras",
    ],
    "Guzelyurt": [
        "Merkez",
        "Bostanci",
        "Yayla",
        "Aydinkoy",
    ],
    "Iskele": [
        "Merkez",
        "Long Beach",
        "Bogaz",
        "Mehmetcik",
    ],
    "Lefke": [
        "Merkez",
        "Gemikonagi",
        "Yedidalga",
        "Cengizkoy",
    ],
}

NC_CITY_CHOICES = [(city, city) for city in NC_CITY_DISTRICT_MAP.keys()]

_all_districts = []
for districts in NC_CITY_DISTRICT_MAP.values():
    for district in districts:
        if district not in _all_districts:
            _all_districts.append(district)

NC_DISTRICT_CHOICES = [(district, district) for district in _all_districts]
