NC_CITY_DISTRICT_MAP = {
    "Lefkoşa": [
        "Hamitköy",
        "Kumsal",
        "Örtaköy",
        "Gönyeli",
        "Metehan",
    ],
    "Girne": [
        "Karakum",
        "Özanköy",
        "Çatalköy",
        "Alsancak",
        "Lapta",
    ],
    "Gazimağusa": [
        "Suriçi",
        "Tuzla",
        "Karakol",
        "Doğu Akdeniz",
        "Maraş",
    ],
    "Güzelyurt": [
        "Merkez",
        "Bostancı",
        "Yayla",
        "Aydınköy",
    ],
    "İskele": [
        "Merkez",
        "Long Beach",
        "Boğaz",
        "Mehmetçik",
    ],
    "Lefke": [
        "Merkez",
        "Gemikonağı",
        "Yedidalga",
        "Cengizköy",
    ],
}

NC_CITY_CHOICES = [(city, city) for city in NC_CITY_DISTRICT_MAP.keys()]

_all_districts = []
for districts in NC_CITY_DISTRICT_MAP.values():
    for district in districts:
        if district not in _all_districts:
            _all_districts.append(district)

NC_DISTRICT_CHOICES = [(district, district) for district in _all_districts]
