from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from store.models import GeoCache as GeoCacheRow

_FLAG_PATTERN = re.compile("[\U0001f1e6-\U0001f1ff]{2}")
_REGIONAL_INDICATORS = frozenset(range(0x1F1E6, 0x1F1FF + 1))
_CANDIDATE_SPLIT = re.compile(r"[/|,•()\n\r]+")
_SKIPPED_MARKS = frozenset({"\ufe0e", "\ufe0f"})

_COUNTRY_ALIASES_RAW = """\
AD andorra|andorran
AE united arab emirates|uae|emirates|emirati
AF afghanistan|afghan
AG antigua and barbuda|antiguan
AI anguilla
AL albania|albanian
AM armenia|armenian
AO angola|angolan
AQ antarctica
AR argentina|argentine|argentinian
AS american samoa
AT austria|austrian|osterreich|österreich
AU australia|australian|aussie
AW aruba
AX aland islands|aland
AZ azerbaijan|azerbaijani
BA bosnia and herzegovina|bosnia|bosnian
BB barbados|barbadian
BD bangladesh|bangladeshi
BE belgium|belgian|belgie|belgië
BF burkina faso|burkinabe
BG bulgaria|bulgarian
BH bahrain|bahraini
BI burundi|burundian
BJ benin|beninese
BL saint barthelemy
BM bermuda
BN brunei
BO bolivia|bolivian
BQ caribbean netherlands
BR brazil|brasil|brazilian
BS bahamas|bahamian
BT bhutan|bhutanese
BV bouvet island
BW botswana|batswana
BY belarus|belarusian
BZ belize|belizean
CA canada|canadian
CC cocos islands
CD democratic republic of the congo|dr congo|congo kinshasa
CF central african republic
CG republic of the congo|congo brazzaville
CH switzerland|swiss|schweiz|suisse|svizzera
CI cote d ivoire|ivory coast|ivorian
CK cook islands
CL chile|chilean
CM cameroon|cameroonian
CN china|chinese|prc
CO colombia|colombian
CR costa rica|rican
CU cuba|cuban
CV cape verde
CW curacao
CX christmas island
CY cyprus|cypriot
CZ czechia|czech republic|czech
DE germany|deutschland|german
DJ djibouti
DK denmark|danish|danmark
DM dominica
DO dominican republic|dominican
DZ algeria|algerian
EC ecuador|ecuadorian
EE estonia|estonian
EG egypt|egyptian
EH western sahara
ER eritrea|eritrean
ES spain|spanish|espana|españa
ET ethiopia|ethiopian
FI finland|finnish|suomi
FJ fiji|fijian
FK falkland islands
FM micronesia|micronesian
FO faroe islands
FR france|french
GA gabon|gabonese
GB united kingdom|uk|great britain|britain
GB england|scotland|wales|northern ireland
GB british|english|scottish|welsh
GD grenada|grenadian
GE georgia|georgian
GF french guiana
GG guernsey
GH ghana|ghanaian
GI gibraltar
GL greenland
GM gambia|gambian
GN guinea|guinean
GP guadeloupe
GQ equatorial guinea
GR greece|greek|hellas
GS south georgia
GT guatemala|guatemalan
GU guam
GW guinea bissau
GY guyana|guyanese
HK hong kong|hongkonger
HM heard island
HN honduras|honduran
HR croatia|croatian|hrvatska
HT haiti|haitian
HU hungary|hungarian|magyarorszag
ID indonesia|indonesian
IE ireland|irish|eire
IL israel|israeli
IM isle of man
IN india|indian
IO british indian ocean territory
IQ iraq|iraqi
IR iran|iranian|persia
IS iceland|icelandic|island
IT italy|italian|italia
JE jersey
JM jamaica|jamaican
JO jordan|jordanian
JP japan|japanese|nippon
KE kenya|kenyan
KG kyrgyzstan|kyrgyz
KH cambodia|cambodian
KI kiribati
KM comoros|comorian
KN saint kitts and nevis
KP north korea|dprk
KR south korea|korea|republic of korea
KW kuwait|kuwaiti
KY cayman islands
KZ kazakhstan|kazakh
LA laos|lao
LB lebanon|lebanese
LC saint lucia
LI liechtenstein
LK sri lanka|sri lankan
LR liberia|liberian
LS lesotho|basotho
LT lithuania|lithuanian
LU luxembourg|luxembourgish
LV latvia|latvian
LY libya|libyan
MA morocco|moroccan
MC monaco|monegasque
MD moldova|moldovan
ME montenegro|montenegrin
MF saint martin
MG madagascar|malagasy
MH marshall islands
MK north macedonia|macedonia|macedonian
ML mali|malian
MM myanmar|burma|burmese
MN mongolia|mongolian
MO macao|macau
MP northern mariana islands
MQ martinique
MR mauritania|mauritanian
MS montserrat
MT malta|maltese
MU mauritius|mauritian
MV maldives|maldivian
MW malawi|malawian
MX mexico|mexican|méxico
MY malaysia|malaysian
MZ mozambique|mozambican
NA namibia|namibian
NC new caledonia
NE niger|nigerien
NF norfolk island
NG nigeria|nigerian
NI nicaragua|nicaraguan
NL netherlands|nederland|holland|dutch
NO norway|norwegian|norge
NP nepal|nepali|nepalese
NR nauru|nauruan
NU niue
NZ new zealand|new zealander|kiwi|aotearoa
OM oman|omani
PA panama|panamanian
PE peru|peruvian|perú
PF french polynesia
PG papua new guinea
PH philippines|filipino|pinoy
PK pakistan|pakistani
PL poland|polish|polska
PM saint pierre and miquelon
PN pitcairn
PR puerto rico|puerto rican
PS palestine|palestinian
PT portugal|portuguese
PW palau|palauan
PY paraguay|paraguayan
QA qatar|qatari
RE reunion|réunion
RO romania|romanian
RS serbia|serbian
RU russia|russian|rossiya
RW rwanda|rwandan
SA saudi arabia|saudi|ksa
SB solomon islands
SC seychelles|seychellois
SD sudan|sudanese
SE sweden|swedish|sverige
SG singapore|singaporean
SH saint helena
SI slovenia|slovenian
SJ svalbard
SK slovakia|slovak
SL sierra leone|sierra leonean
SM san marino
SN senegal|senegalese
SO somalia|somali
SR suriname|surinamese
SS south sudan
ST sao tome and principe
SV el salvador|salvadoran
SX sint maarten
SY syria|syrian
SZ eswatini|swaziland|swazi
TC turks and caicos islands
TD chad|chadian
TF french southern territories
TG togo|togolese
TH thailand|thai
TJ tajikistan|tajik
TK tokelau
TL timor leste|east timor
TM turkmenistan|turkmen
TN tunisia|tunisian
TO tonga|tongan
TR turkey|turkish|turkiye|türkiye
TT trinidad and tobago|trinidadian
TV tuvalu
TW taiwan|taiwanese
TZ tanzania|tanzanian
UA ukraine|ukrainian
UG uganda|ugandan
UM united states minor outlying islands
US united states|usa|america|united states of america
US american
UY uruguay|uruguayan
UZ uzbekistan|uzbek
VA vatican city|holy see
VC saint vincent and the grenadines
VE venezuela|venezuelan
VG british virgin islands
VI united states virgin islands|us virgin islands
VN vietnam|vietnamese
VU vanuatu
WF wallis and futuna
WS samoa|samoan
YE yemen|yemeni
YT mayotte
ZA south africa|south african
ZM zambia|zambian
ZW zimbabwe|zimbabwean
"""

_GAZETTEER_RAW = """\
AE dubai|abu dhabi|sharjah|al ain|ajman|ras al khaimah
AF kabul
AO luanda
AR buenos aires|cordoba|córdoba|rosario|mendoza|la plata
AR tucuman|tucumán|mar del plata|salta|santa fe
AT vienna|wien|graz|linz|salzburg|innsbruck
AU sydney|melbourne|brisbane|perth|adelaide|gold coast
AU canberra|wollongong|geelong|hobart|darwin|cairns|toowoomba
AZ baku
BD dhaka|chittagong|chattogram|khulna|rajshahi|sylhet
BE brussels|bruxelles|antwerp|antwerpen|ghent|gent
BE charleroi|liege|liège|bruges|brugge
BF ouagadougou
BG sofia|plovdiv|varna|burgas|ruse
BH manama
BO la paz|santa cruz|cochabamba
BR sao paulo|são paulo|rio de janeiro|brasilia|brasília
BR salvador|fortaleza|belo horizonte|manaus|curitiba|recife
BR porto alegre|belem|belém|goiania|goiânia|guarulhos
BR campinas|sao luis|maceio|natal|joao pessoa|teresina
BR cuiaba|campo grande|florianopolis|vitoria
BY minsk
CA toronto|montreal|vancouver|calgary|edmonton|ottawa
CA winnipeg|quebec city|quebec|hamilton|kitchener|mississauga
CA windsor|victoria|halifax|regina|saskatoon
CH zurich|zürich|geneva|geneve|genève|basel
CH bern|berne|lausanne|lucerne
CI abidjan|yamoussoukro
CL santiago|valparaiso|valparaíso|concepcion|concepción
CL la serena|antofagasta|temuco
CM douala|yaounde
CN beijing|shanghai|guangzhou|shenzhen|chengdu|chongqing
CN wuhan|xi'an|xian|hangzhou|nanjing|tianjin
CN suzhou|dongguan|qingdao|zhengzhou|changsha|kunming
CN dalian|xiamen|hefei|fuzhou|harbin|jinan
CN nanning|taiyuan|shijiazhuang|urumqi|lanzhou|foshan
CO bogota|bogotá|medellin|medellín|cali
CO barranquilla|cartagena|bucaramanga|cucuta
CR san jose costa rica
CU havana|habana
CZ prague|praha|brno|ostrava|plzen|plzeň|liberec
DE berlin|munich|munchen|münchen|hamburg|frankfurt
DE cologne|koln|köln|stuttgart|dusseldorf|düsseldorf
DE dortmund|essen|leipzig|dresden|hanover|hannover
DE nuremberg|nürnberg|bremen|bonn|mannheim|karlsruhe
DE munster|münster
DK copenhagen|kobenhavn|københavn|aarhus|odense|aalborg|esbjerg
DO santo domingo
DZ algiers|oran|constantine
EC quito|guayaquil|cuenca
EE tallinn|tartu
EG cairo|alexandria|giza|luxor|aswan
ES madrid|barcelona|valencia|seville|sevilla|zaragoza
ES malaga|málaga|murcia|palma|bilbao|alicante
ES valladolid|vigo|gijon|gijón|granada|oviedo
ES la coruna|a coruna|santa cruz de tenerife
ET addis ababa|dire dawa
FI helsinki|espoo|tampere|vantaa|oulu|turku|jvaskyla
FR paris|marseille|lyon|toulouse|nice|nantes
FR montpellier|strasbourg|bordeaux|lille|rennes|reims
FR toulon|grenoble|dijon|angers|nimes|nîmes
FR villeurbanne|saint-etienne|saint etienne
GB london|birmingham|manchester|glasgow|liverpool|leeds
GB sheffield|edinburgh|bristol|cardiff|belfast|newcastle
GB nottingham|leicester|southampton|portsmouth|brighton
GB aberdeen|cambridge|oxford|york|reading|swansea|plymouth|coventry
GE tbilisi|batumi
GH accra|kumasi|tamale
GR athens|athina|thessaloniki|patras|heraklion|larissa
GT guatemala city
HK hong kong
HR zagreb|split|rijeka|osijek
HU budapest|debrecen|szeged|miskolc|pecs|pécs
ID jakarta|surabaya|bandung|medan|semarang|makassar
ID palembang|tangerang|depok|bekasi|denpasar
IE dublin|cork|limerick|galway|waterford
IL tel aviv|jerusalem|haifa|beersheba|netanya
IN mumbai|bombay|delhi|new delhi|bangalore|bengaluru
IN hyderabad|chennai|madras|kolkata|calcutta|pune
IN ahmedabad|surat|jaipur|lucknow|kanpur|nagpur|indore
IN thane|bhopal|visakhapatnam|patna|vadodara|ludhiana|agra
IN nashik|faridabad|meerut|rajkot|varanasi|srinagar
IN aurangabad|amritsar|allahabad|prayagraj|ranchi|howrah
IN coimbatore|jabalpur|gwalior|vijayawada|jodhpur|madurai
IN raipur|guwahati|chandigarh|mysore|mysuru|bareilly
IN aligarh|tiruppur|moradabad|jalandhar|bhubaneswar|salem
IN warangal|guntur|bhiwandi|saharanpur|gorakhpur|bikaner
IN amravati|noida|jamshedpur|bhilai|cuttack|firozabad
IN kochi|cochin|nellore|bhavnagar|dehradun|durgapur
IN asansol|rourkela|nanded|kolhapur|ajmer|akola
IN gulbarga|jamnagar|ujjain|loni|siliguri|jhansi
IN ulhasnagar|jammu|sangli|mangalore|erode|belgaum
IN ambattur|tirunelveli|malegaon|gaya|jalgaon|udaipur
IN davanagere|kozhikode|calicut|kurnool|rajahmundry|bokaro
IN bellary|patiala|agartala|bhagalpur|latur|dhule|rohtak
IQ baghdad|basra|mosul|erbil
IR tehran|mashhad|isfahan|shiraz|tabriz|qom|ahvaz
IS reykjavik
IT rome|roma|milan|milano|naples|napoli|turin|torino
IT palermo|genoa|genova|bologna|florence|firenze|bari
IT catania|venice|venezia|verona|messina|padua|padova
IT trieste|brescia|parma|cagliari
JM kingston
JO amman
JP tokyo|osaka|yokohama|nagoya|sapporo|fukuoka
JP kobe|kyoto|kawasaki|saitama|hiroshima|sendai
JP chiba|kitakyushu|sakai
KE nairobi|mombasa|kisumu|nakuru
KG bishkek
KH phnom penh
KR seoul|busan|incheon|daegu|daejeon|gwangju|suwon|ulsan|changwon
KW kuwait city
KZ almaty|astana|nur-sultan
LB beirut
LK colombo|kandy
LT vilnius|kaunas
LV riga
LY tripoli|benghazi
MA casablanca|rabat|marrakesh|fes|tangier
MD chisinau
MM yangon|mandalay
MU port louis
MX mexico city|guadalajara|monterrey|puebla|tijuana|leon
MX juarez|ciudad juarez|zapopan|merida|mérida|cancun
MX toluca|queretaro|querétaro
MY kuala lumpur|george town|johor bahru|ipoh
MY shah alam|petaling jaya|kuching|kota kinabalu
MZ maputo
NG lagos|abuja|kano|ibadan|port harcourt|benin city|kaduna|enugu
NL amsterdam|rotterdam|the hague|den haag|utrecht|eindhoven
NL groningen|tilburg|almere|breda|nijmegen|haarlem
NO oslo|bergen|trondheim|stavanger|drammen|fredrikstad
NP kathmandu|pokhara
NZ auckland|wellington|christchurch|tauranga|dunedin
OM muscat
PA panama city
PE lima|arequipa|trujillo|chiclayo|cusco|piura
PH manila|quezon city|davao|cagayan de oro|cebu|zamboanga|makati
PK karachi|lahore|faisalabad|rawalpindi|islamabad|multan
PK gujranwala|peshawar|quetta
PL warsaw|warszawa|krakow|kraków|lodz|łódź|wroclaw|wrocław
PL poznan|poznań|gdansk|gdańsk|szczecin|bydgoszcz
PL lublin|katowice|bialystok
PT lisbon|lisboa|porto|braga|coimbra|faro|funchal
PY asuncion
QA doha
RO bucharest|bucuresti|cluj-napoca|cluj napoca|timisoara
RO timișoara|iasi|iași|constanta|constanța|brasov|brașov|craiova
RS belgrade|novi sad|nis|niš|kragujevac
RU moscow|moskva|saint petersburg|st petersburg|novosibirsk
RU yekaterinburg|kazan|nizhny novgorod|chelyabinsk|samara
RU omsk|rostov-on-don|rostov on don|ufa|krasnoyarsk
RU voronezh|perm|volgograd
RW kigali
SA riyadh|jeddah|mecca|medina|dammam|khobar|tabuk
SE stockholm|gothenburg|goteborg|göteborg|malmo|malmö
SE uppsala|vasteras|västerås|orebro|örebro
SE linkoping|linköping|helsingborg
SG singapore
SI ljubljana|maribor
SK bratislava|kosice|košice|presov|prešov
SN dakar
TH bangkok|chiang mai|pattaya|phuket|khon kaen|hat yai
TN tunis
TR istanbul|ankara|izmir|bursa|antalya|adana|gaziantep
TR konya|kayseri
TT port of spain
TW taipei|kaohsiung|taichung|tainan
TZ dar es salaam|dodoma|arusha|mwanza
UA kyiv|kiev|kharkiv|odesa|odessa|dnipro|donetsk|lviv
UG kampala
US new york|new york city|los angeles|chicago|houston|phoenix
US philadelphia|san antonio|san diego|dallas|san jose|austin
US jacksonville|fort worth|columbus|charlotte|indianapolis
US san francisco|seattle|denver|washington|boston|el paso
US nashville|detroit|oklahoma city|portland|las vegas|memphis
US louisville|baltimore|milwaukee|albuquerque|tucson|fresno
US sacramento|kansas city|atlanta|omaha|raleigh|miami
US minneapolis|tampa|new orleans|cleveland|honolulu
US pittsburgh|st louis|saint louis|cincinnati|orlando|buffalo
US salt lake city|boise|richmond|spokane|des moines|rochester
UY montevideo
UZ tashkent|samarkand
VE caracas|maracaibo|barquisimeto|maracay
VN ho chi minh city|ho chi minh|saigon|hanoi|haiphong|ha noi
VN da nang|danang|can tho|bien hoa|hue
ZA johannesburg|cape town|durban|pretoria
ZA port elizabeth|bloemfontein|soweto
ZM lusaka
ZW harare|bulawayo
"""

_CC_TLDS = {
    "ar": "AR",
    "at": "AT",
    "au": "AU",
    "be": "BE",
    "br": "BR",
    "ca": "CA",
    "ch": "CH",
    "cn": "CN",
    "cz": "CZ",
    "de": "DE",
    "dk": "DK",
    "es": "ES",
    "fi": "FI",
    "fr": "FR",
    "gb": "GB",
    "gh": "GH",
    "ie": "IE",
    "il": "IL",
    "in": "IN",
    "it": "IT",
    "jp": "JP",
    "ke": "KE",
    "kr": "KR",
    "mx": "MX",
    "ng": "NG",
    "nl": "NL",
    "no": "NO",
    "nz": "NZ",
    "pl": "PL",
    "pt": "PT",
    "ru": "RU",
    "se": "SE",
    "tr": "TR",
    "ua": "UA",
    "uk": "GB",
    "us": "US",
    "za": "ZA",
}

_TZ_BANDS: dict[float, frozenset[str]] = {
    -10.0: frozenset({"US", "PF", "CK"}),
    -9.0: frozenset({"US", "PF"}),
    -8.0: frozenset({"US", "CA", "MX"}),
    -5.0: frozenset({"US", "CA", "CO", "PE", "EC", "CU"}),
    -3.0: frozenset({"BR", "AR", "UY"}),
    0.0: frozenset({"GB", "IE", "PT", "IS", "GH", "SN", "CI"}),
    1.0: frozenset({"DE", "FR", "IT", "ES", "NL", "BE", "AT", "PL", "CH", "NG"}),
    2.0: frozenset({"ZA", "EG", "GR", "FI", "RO", "IL", "UA"}),
    3.0: frozenset({"RU", "SA", "TR", "KE", "ET", "QA"}),
    3.5: frozenset({"IR"}),
    4.5: frozenset({"AF"}),
    5.5: frozenset({"IN", "LK"}),
    5.75: frozenset({"NP"}),
    6.5: frozenset({"MM", "CC"}),
    8.0: frozenset({"CN", "SG", "MY", "PH", "TW", "HK", "AU"}),
    9.0: frozenset({"JP", "KR"}),
    9.5: frozenset({"AU"}),
    10.0: frozenset({"AU", "PG"}),
    12.0: frozenset({"NZ", "FJ", "TV", "WF", "MH", "NR", "KI"}),
}


@dataclass(frozen=True)
class GeoResult:
    country_iso: str | None
    confidence: str
    raw_location: str | None


def _clean(text: str, *, keep_flags: bool) -> str:
    parts: list[str] = []
    for char in unicodedata.normalize("NFC", text.lower()):
        category = unicodedata.category(char)
        keep = char.isalnum() or char.isspace() or category.startswith("M")
        if keep and char not in _SKIPPED_MARKS:
            parts.append(char)
        elif keep_flags and ord(char) in _REGIONAL_INDICATORS:
            parts.append(char)
        else:
            parts.append(" ")
    return " ".join("".join(parts).split())


def _build_aliases(raw: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in raw.strip().splitlines():
        code, _, rest = line.strip().partition(" ")
        table[code.lower()] = code
        for alias in rest.split("|"):
            cleaned = _clean(alias, keep_flags=False)
            if cleaned:
                table[cleaned] = code
    return table


def _build_gazetteer(raw: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in raw.strip().splitlines():
        code, _, rest = line.strip().partition(" ")
        for city in rest.split("|"):
            cleaned = _clean(city, keep_flags=False)
            if cleaned:
                table.setdefault(cleaned, code)
    return table


_COUNTRY_ALIASES = _build_aliases(_COUNTRY_ALIASES_RAW)
_GAZETTEER = _build_gazetteer(_GAZETTEER_RAW)


def normalize_location(raw: str) -> str:
    if not isinstance(raw, str):
        return _clean(str(raw), keep_flags=True)
    return _clean(raw, keep_flags=True)


def _as_raw(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


def _first_flag_iso(raw: str) -> str | None:
    match = _FLAG_PATTERN.search(raw)
    if match is None:
        return None
    first, second = match.group()
    return chr(ord(first) - 0x1F1E6 + 0x41) + chr(ord(second) - 0x1F1E6 + 0x41)


def _split_candidates(raw: str) -> list[str]:
    candidates: list[str] = []
    for part in _CANDIDATE_SPLIT.split(raw):
        without_flags = _FLAG_PATTERN.sub(" ", part)
        cleaned = _clean(without_flags, keep_flags=False)
        if cleaned:
            candidates.append(cleaned)
    return candidates


def _alias_prefix(candidate: str) -> str | None:
    words = candidate.split()
    for cut in range(len(words) - 1, 0, -1):
        phrase = " ".join(words[:cut])
        if len(phrase) < 3:
            continue
        hit = _COUNTRY_ALIASES.get(phrase)
        if hit is not None:
            return hit
    return None


def _city_exact(candidate: str, gazetteer: Mapping[str, str] | None) -> str | None:
    if gazetteer is not None:
        hit = gazetteer.get(candidate)
        if hit is not None:
            return hit
    return _GAZETTEER.get(candidate)


def _city_prefix(candidate: str, gazetteer: Mapping[str, str] | None) -> str | None:
    words = candidate.split()
    for cut in range(len(words) - 1, 0, -1):
        phrase = " ".join(words[:cut])
        if gazetteer is not None:
            hit = gazetteer.get(phrase)
            if hit is not None:
                return hit
        hit = _GAZETTEER.get(phrase)
        if hit is not None:
            return hit
    return None


def _valid_iso(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) == 2 and code.isascii() and code.isalpha():
        return code
    return None


def _blog_iso(blog: str | None) -> str | None:
    if not isinstance(blog, str) or not blog.strip():
        return None
    url = blog if "://" in blog else f"https://{blog}"
    host = urlparse(url).hostname or ""
    if "." not in host:
        return None
    return _CC_TLDS.get(host.rsplit(".", 1)[-1].lower())


def _tz_iso(tz_offset: float | None) -> str | None:
    if tz_offset is None:
        return None
    try:
        offset = float(tz_offset)
    except (TypeError, ValueError, OverflowError):
        return None
    scaled = offset * 4
    if not math.isfinite(offset) or not math.isfinite(scaled):
        return None
    key = round(scaled) / 4
    codes = _TZ_BANDS.get(key)
    if codes is not None and len(codes) == 1:
        return next(iter(codes))
    return None


def resolve_location(
    raw: str | None,
    *,
    gazetteer: Mapping[str, str] | None = None,
    geocoder: Callable[[str], str | None] | None = None,
    blog: str | None = None,
    tz_offset: float | None = None,
) -> GeoResult:
    original = _as_raw(raw)
    if original is not None:
        flag_iso = _first_flag_iso(original)
        if flag_iso is not None:
            return GeoResult(flag_iso, "exact-iso", original)
        candidates = _split_candidates(original)
    else:
        candidates = []
    for candidate in candidates:
        iso = _COUNTRY_ALIASES.get(candidate)
        if iso is not None:
            return GeoResult(iso, "name", original)
    for candidate in candidates:
        iso = _alias_prefix(candidate)
        if iso is not None:
            return GeoResult(iso, "name", original)
    for candidate in candidates:
        iso = _city_exact(candidate, gazetteer)
        if iso is not None:
            return GeoResult(iso, "gazetteer-city", original)
    for candidate in candidates:
        iso = _city_prefix(candidate, gazetteer)
        if iso is not None:
            return GeoResult(iso, "gazetteer-city", original)
    if geocoder is not None:
        for candidate in candidates:
            iso = _valid_iso(geocoder(candidate))
            if iso is not None:
                return GeoResult(iso, "geocoder", original)
    weak = _blog_iso(blog) if blog is not None else None
    if weak is None:
        weak = _tz_iso(tz_offset)
    if weak is not None:
        return GeoResult(weak, "weak", original)
    return GeoResult(None, "unmatched", original)


class GeoCache:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, normalized: str) -> GeoResult | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    GeoCacheRow.country_iso,
                    GeoCacheRow.confidence,
                    GeoCacheRow.raw_sample,
                ).where(GeoCacheRow.normalized == normalized)
            ).first()
        if row is None:
            return None
        return GeoResult(row.country_iso, row.confidence, row.raw_sample)

    def put(self, normalized: str, result: GeoResult) -> None:
        base = pg_insert(GeoCacheRow).values(
            normalized=normalized,
            country_iso=result.country_iso,
            confidence=result.confidence,
            raw_sample=result.raw_location,
            hits=1,
        )
        statement = base.on_conflict_do_update(
            index_elements=[GeoCacheRow.normalized],
            set_={
                "country_iso": base.excluded.country_iso,
                "confidence": base.excluded.confidence,
                "raw_sample": base.excluded.raw_sample,
                "hits": GeoCacheRow.hits + 1,
                "updated_at": sa.func.now(),
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)


def resolve_owner(
    engine: Engine,
    raw_location: str | None,
    *,
    gazetteer: Mapping[str, str] | None = None,
    geocoder: Callable[[str], str | None] | None = None,
    blog: str | None = None,
    tz_offset: float | None = None,
) -> GeoResult:
    cache = GeoCache(engine)
    raw = _as_raw(raw_location)
    key = normalize_location(raw) if raw is not None else ""
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = resolve_location(
        raw,
        gazetteer=gazetteer,
        geocoder=geocoder,
        blog=blog,
        tz_offset=tz_offset,
    )
    cache.put(key, result)
    return result
