"""Known canonical artist → movement mappings.

Used as Priority 2 evidence in movement classification.

Important constraints:
  - An artist MAY belong to multiple movements.
  - An artist MAY transition between movements across their career.
  - These mappings indicate PRIMARY associations, not exclusivity.
  - Keys are normalized: lowercased, unaccented, whitespace-collapsed.
  - Lookup tolerates museum formatting ("Monet, Claude (French, 1840-1926)").

Every entry here is a real, documented association. When in doubt, an artist is
omitted rather than guessed: a wrong movement is worse than no movement.
"""

from __future__ import annotations

import re

from pinterest_art_bot.domain.movement import ArtMovement
from pinterest_art_bot.domain.normalization import strip_accents

# Mapping: normalized artist name → associated movements (first = primary)
ARTIST_MOVEMENTS: dict[str, list[ArtMovement]] = {
    # ──── Ancient, Medieval & Gothic ──────────────────────────────────
    "agnolo gaddi": [ArtMovement.MEDIEVAL, ArtMovement.GOTHIC],
    "andrea di bartolo": [ArtMovement.GOTHIC],
    "barnaba da modena": [ArtMovement.GOTHIC],
    "lippo di benivieni": [ArtMovement.MEDIEVAL, ArtMovement.EARLY_RENAISSANCE],
    # ──── Early & High Renaissance ────────────────────────────────────
    "ambrogio bevilacqua": [ArtMovement.RENAISSANCE],
    "andrea del castagno": [ArtMovement.EARLY_RENAISSANCE],
    "andrea del sarto": [ArtMovement.HIGH_RENAISSANCE],
    "andrea mantegna": [ArtMovement.EARLY_RENAISSANCE],
    "bartolomeo montagna": [ArtMovement.RENAISSANCE],
    "cima da conegliano": [ArtMovement.RENAISSANCE],
    "corneille de lyon": [ArtMovement.RENAISSANCE],
    "correggio": [ArtMovement.HIGH_RENAISSANCE],
    "fra angelico": [ArtMovement.EARLY_RENAISSANCE],
    "giovanni bellini": [ArtMovement.EARLY_RENAISSANCE],
    "jacopo bassano": [ArtMovement.RENAISSANCE, ArtMovement.MANNERISM],
    "leonardo da vinci": [ArtMovement.HIGH_RENAISSANCE],
    "luca della robbia": [ArtMovement.EARLY_RENAISSANCE],
    "masaccio": [ArtMovement.EARLY_RENAISSANCE],
    "michelangelo": [ArtMovement.HIGH_RENAISSANCE, ArtMovement.MANNERISM],
    "paolo ucello": [ArtMovement.EARLY_RENAISSANCE],
    "pietro perugino": [ArtMovement.EARLY_RENAISSANCE],
    "raphael": [ArtMovement.HIGH_RENAISSANCE],
    "raphael sanzio": [ArtMovement.HIGH_RENAISSANCE],
    "sandro botticelli": [ArtMovement.EARLY_RENAISSANCE],
    "titian": [ArtMovement.HIGH_RENAISSANCE],
    "tiziano": [ArtMovement.HIGH_RENAISSANCE],
    "tiziano vecellio": [ArtMovement.HIGH_RENAISSANCE],
    # ──── Northern Renaissance & Mannerism ────────────────────────────
    "albrecht durer": [ArtMovement.NORTHERN_RENAISSANCE],
    "conrad faber von kreuznach": [ArtMovement.NORTHERN_RENAISSANCE],
    "dieric bouts": [ArtMovement.NORTHERN_RENAISSANCE],
    "domenikos theotokopoulos": [ArtMovement.MANNERISM],
    "el greco": [ArtMovement.MANNERISM],
    "francesco del tadda": [ArtMovement.MANNERISM],
    "hieronymus bosch": [ArtMovement.NORTHERN_RENAISSANCE],
    "jan van eyck": [ArtMovement.NORTHERN_RENAISSANCE],
    "jan van sc eyffert": [ArtMovement.NORTHERN_RENAISSANCE],
    "joos van cleve": [ArtMovement.NORTHERN_RENAISSANCE],
    "lucas van leyden": [ArtMovement.NORTHERN_RENAISSANCE],
    "parmigianino": [ArtMovement.MANNERISM],
    "petrus christus": [ArtMovement.NORTHERN_RENAISSANCE],
    "quinten massys": [ArtMovement.NORTHERN_RENAISSANCE],
    "robert campin": [ArtMovement.NORTHERN_RENAISSANCE],
    "rogier van der weyden": [ArtMovement.NORTHERN_RENAISSANCE],
    "rosso fiorentino": [ArtMovement.MANNERISM],
    # ──── Baroque ─────────────────────────────────────────────────────
    "abraham janssen van nuyssen": [ArtMovement.BAROQUE],
    "anthony van dyck": [ArtMovement.BAROQUE],
    "artemisia gentileschi": [ArtMovement.BAROQUE],
    "bernard vaillant": [ArtMovement.BAROQUE],
    "caravaggio": [ArtMovement.BAROQUE],
    "carel fabritius": [ArtMovement.BAROQUE],
    "claes moeyaert": [ArtMovement.BAROQUE],
    "claude lorrain": [ArtMovement.BAROQUE],
    "diego velazquez": [ArtMovement.BAROQUE],
    "frans hals": [ArtMovement.BAROQUE],
    "gerbrand van den eeckhout": [ArtMovement.BAROQUE],
    "gerrit van honthorst": [ArtMovement.BAROQUE],
    "gian lorenzo bernini": [ArtMovement.BAROQUE],
    "godfried schalcken": [ArtMovement.BAROQUE],
    "govaert flinck": [ArtMovement.BAROQUE],
    "guido reni": [ArtMovement.BAROQUE],
    "guidobaldo ferrarese": [ArtMovement.BAROQUE],
    "hercules seghers": [ArtMovement.BAROQUE],
    "jacob van ruisdael": [ArtMovement.BAROQUE],
    "jacob van spreeuwen": [ArtMovement.BAROQUE],
    "jan baptist weenix": [ArtMovement.BAROQUE],
    "jan both": [ArtMovement.BAROQUE],
    "jan ekels the younger": [ArtMovement.BAROQUE],
    "jan steen": [ArtMovement.BAROQUE],
    "jan van der heyden": [ArtMovement.BAROQUE],
    "jan vermeer": [ArtMovement.BAROQUE],
    "jan weenix": [ArtMovement.BAROQUE],
    "johannes vermeer": [ArtMovement.BAROQUE],
    "john michael wright": [ArtMovement.BAROQUE],
    "jurgen ovens": [ArtMovement.BAROQUE],
    "jusepe de ribera": [ArtMovement.BAROQUE],
    "jusepe de ribera y jusep": [ArtMovement.BAROQUE],
    "leonaert bramer": [ArtMovement.BAROQUE],
    "michiel sweerts": [ArtMovement.BAROQUE],
    "nicolas maes": [ArtMovement.BAROQUE],
    "peter paul rubens": [ArtMovement.BAROQUE],
    "philips van dijck": [ArtMovement.BAROQUE],
    "pieter de hooch": [ArtMovement.BAROQUE],
    "pieter van laer": [ArtMovement.BAROQUE],
    "rembrandt": [ArtMovement.BAROQUE],
    "rembrandt van rijn": [ArtMovement.BAROQUE],
    "salomon koninck": [ArtMovement.BAROQUE],
    "salomon ruisdael": [ArtMovement.BAROQUE],
    # ──── Rococo ──────────────────────────────────────────────────────
    "alexis grimou": [ArtMovement.ROCOCO],
    "antoine watteau": [ArtMovement.ROCOCO],
    "charles dominique joseph eisen": [ArtMovement.ROCOCO],
    "fragonard": [ArtMovement.ROCOCO],
    "francois boucher": [ArtMovement.ROCOCO],
    "giambattista tiepolo": [ArtMovement.ROCOCO],
    "giovanni battista crespi": [ArtMovement.ROCOCO],
    "giovanni battista piazzetta": [ArtMovement.ROCOCO],
    "giovanni battista tiepolo": [ArtMovement.ROCOCO],
    "jean honore fragonard": [ArtMovement.ROCOCO],
    "jean simeon chardin": [ArtMovement.ROCOCO],
    "johann zoffany": [ArtMovement.ROCOCO],
    "joseph wright": [ArtMovement.ROCOCO, ArtMovement.NEOCLASSICISM],
    "joseph wright of derby": [ArtMovement.ROCOCO],
    "nicolas de largilliere": [ArtMovement.ROCOCO],
    "pierre antoine baudouin": [ArtMovement.ROCOCO],
    "william hogarth": [ArtMovement.ROCOCO],
    # ──── Neoclassicism & Romanticism ─────────────────────────────────
    "aivazovsky": [ArtMovement.ROMANTICISM],
    "angelica kauffman": [ArtMovement.NEOCLASSICISM],
    "anne louis girodet": [ArtMovement.NEOCLASSICISM],
    "antonio canova": [ArtMovement.NEOCLASSICISM],
    "benjamin west": [ArtMovement.NEOCLASSICISM],
    "caspar david friedrich": [ArtMovement.ROMANTICISM],
    "charles bird king": [ArtMovement.NEOCLASSICISM],
    "charles lasch": [ArtMovement.NEOCLASSICISM],
    "copley john singleton": [ArtMovement.NEOCLASSICISM, ArtMovement.ROMANTICISM],
    "elenore rosenberg": [ArtMovement.NEOCLASSICISM],
    "eugene delacroix": [ArtMovement.ROMANTICISM],
    "francisco de goya": [ArtMovement.ROMANTICISM],
    "francisco goya": [ArtMovement.ROMANTICISM],
    "francisco jose de goya y lucientes": [ArtMovement.ROMANTICISM],
    "george catlin": [ArtMovement.ROMANTICISM],
    "gilbert stuart": [ArtMovement.NEOCLASSICISM],
    "henry middleton": [ArtMovement.NEOCLASSICISM],
    "ivan aivazovsky": [ArtMovement.ROMANTICISM],
    "j.m.w. turner": [ArtMovement.ROMANTICISM],
    "jacques louis david": [ArtMovement.NEOCLASSICISM],
    "james m. whistler": [ArtMovement.ROMANTICISM],
    "james mcneill whistler": [ArtMovement.ROMANTICISM],
    "jean auguste dominique ingres": [ArtMovement.NEOCLASSICISM],
    "john constable": [ArtMovement.ROMANTICISM],
    "john hearman": [ArtMovement.NEOCLASSICISM],
    "john singer sargent": [ArtMovement.ROMANTICISM, ArtMovement.IMPRESSIONISM],
    "john singleton copley": [ArtMovement.NEOCLASSICISM, ArtMovement.ROMANTICISM],
    "john trumbull": [ArtMovement.NEOCLASSICISM, ArtMovement.ROMANTICISM],
    "john vanderlyn": [ArtMovement.NEOCLASSICISM],
    "joseph mallord william turner": [ArtMovement.ROMANTICISM],
    "raphael peale": [ArtMovement.NEOCLASSICISM, ArtMovement.ROMANTICISM],
    "rembrandt peale": [ArtMovement.NEOCLASSICISM],
    "sarah miriam peale": [ArtMovement.NEOCLASSICISM],
    "theodore gericault": [ArtMovement.ROMANTICISM],
    "thomas sully": [ArtMovement.ROMANTICISM],
    "titian ramsey peale": [ArtMovement.NEOCLASSICISM],
    "west benjamin": [ArtMovement.NEOCLASSICISM, ArtMovement.ROMANTICISM],
    "william etty": [ArtMovement.ROMANTICISM],
    "william peale": [ArtMovement.NEOCLASSICISM],
    "william turner": [ArtMovement.ROMANTICISM],
    # ──── Realism & Impressionism ─────────────────────────────────────
    "alfred sisley": [ArtMovement.IMPRESSIONISM],
    "berthe morisot": [ArtMovement.IMPRESSIONISM],
    "camille pissarro": [ArtMovement.IMPRESSIONISM, ArtMovement.NEO_IMPRESSIONISM],
    "childe hassam": [ArtMovement.IMPRESSIONISM],
    "claude monet": [ArtMovement.IMPRESSIONISM],
    "constant troyon": [ArtMovement.REALISM],
    "edgar degas": [ArtMovement.IMPRESSIONISM],
    "edouard manet": [ArtMovement.IMPRESSIONISM],
    "eva gonzales": [ArtMovement.IMPRESSIONISM],
    "frederick carl frieseke": [ArtMovement.IMPRESSIONISM],
    "gustave caillebotte": [ArtMovement.IMPRESSIONISM],
    "gustave courbet": [ArtMovement.REALISM],
    "honore daumier": [ArtMovement.REALISM],
    "ilya repin": [ArtMovement.REALISM],
    "jean francois millet": [ArtMovement.REALISM],
    "jules bastien lepage": [ArtMovement.REALISM],
    "leon bonnat": [ArtMovement.REALISM],
    "mary cassatt": [ArtMovement.IMPRESSIONISM],
    "maurice prendergast": [ArtMovement.IMPRESSIONISM],
    "pierre auguste renoir": [ArtMovement.IMPRESSIONISM],
    "rose bonheur": [ArtMovement.REALISM],
    "theodore robinson": [ArtMovement.IMPRESSIONISM],
    "theodore rousseau": [ArtMovement.REALISM],
    "thomas eakins": [ArtMovement.REALISM],
    "vasily surikov": [ArtMovement.REALISM],
    "winslow homer": [ArtMovement.REALISM],
    # ──── Later movements ─────────────────────────────────────────────
    "a. m. cassandre": [ArtMovement.ART_DECO],
    "adolph gottlieb": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "aeneas macke": [ArtMovement.EXPRESSIONISM],
    "agnes martin": [ArtMovement.MINIMALISM],
    "albert gleizes": [ArtMovement.CUBISM],
    "albert marquet": [ArtMovement.FAUVISM],
    "alphonse mucha": [ArtMovement.ART_NOUVEAU],
    "amadeo de souza cardoso": [ArtMovement.CUBISM],
    "andre derain": [ArtMovement.FAUVISM],
    "andy warhol": [ArtMovement.POP_ART],
    "arnold boecklin": [ArtMovement.SYMBOLISM],
    "arshile gorky": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "barbara kruger": [ArtMovement.DADA],
    "carl andre": [ArtMovement.MINIMALISM],
    "carl larsson": [ArtMovement.ART_NOUVEAU],
    "carlo carra": [ArtMovement.FUTURISM],
    "chaim soutine": [ArtMovement.EXPRESSIONISM],
    "charles angrand": [ArtMovement.NEO_IMPRESSIONISM],
    "claes oldenburg": [ArtMovement.POP_ART],
    "claude price": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "constantin brancusi": [ArtMovement.CUBISM],
    "dan flavin": [ArtMovement.MINIMALISM],
    "donald judd": [ArtMovement.MINIMALISM],
    "dorothea tanning": [ArtMovement.SURREALISM],
    "edvard munch": [ArtMovement.EXPRESSIONISM],
    "egon schiele": [ArtMovement.EXPRESSIONISM],
    "el lissitzky": [ArtMovement.CONSTRUCTIVISM],
    "ernst ludwig kirchner": [ArtMovement.EXPRESSIONISM],
    "eugene grasset": [ArtMovement.ART_NOUVEAU],
    "ferdinand hodler": [ArtMovement.ART_NOUVEAU, ArtMovement.SYMBOLISM],
    "fernand khnopff": [ArtMovement.SYMBOLISM],
    "fernard leger": [ArtMovement.CUBISM],
    "frank stella": [ArtMovement.MINIMALISM],
    "franz kline": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "frida kahlo": [ArtMovement.SURREALISM],
    "georges braque": [ArtMovement.CUBISM],
    "georges lemmen": [ArtMovement.NEO_IMPRESSIONISM],
    "georges rouault": [ArtMovement.EXPRESSIONISM],
    "georges seurat": [ArtMovement.NEO_IMPRESSIONISM, ArtMovement.POST_IMPRESSIONISM],
    "giacomo balla": [ArtMovement.FUTURISM],
    "giorgio de chirico": [ArtMovement.ART_DECO, ArtMovement.SURREALISM],
    "giovanni segantini": [ArtMovement.ART_NOUVEAU],
    "gustav klimt": [ArtMovement.ART_NOUVEAU],
    "gustave moreau": [ArtMovement.SYMBOLISM],
    "hans arp": [ArtMovement.DADA],
    "hans hofmann": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "helen frankenthaler": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "henri de toulouse lautrec": [ArtMovement.POST_IMPRESSIONISM],
    "henri matisse": [ArtMovement.FAUVISM, ArtMovement.MODERNISM],
    "jacek malczewski": [ArtMovement.SYMBOLISM],
    "jackson pollock": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "james rosenquist": [ArtMovement.POP_ART],
    "jan jonson": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "jasper johns": [ArtMovement.POP_ART],
    "jean metzinger": [ArtMovement.CUBISM],
    "joan miro": [ArtMovement.SURREALISM],
    "juan gris": [ArtMovement.CUBISM],
    "jules cheret": [ArtMovement.ART_NOUVEAU],
    "karl schmidt rottluff": [ArtMovement.EXPRESSIONISM],
    "kathe kolwitz": [ArtMovement.EXPRESSIONISM],
    "kazimir malevich": [ArtMovement.CONSTRUCTIVISM],
    "kurt schwitters": [ArtMovement.DADA, ArtMovement.CONSTRUCTIVISM],
    "le corbusier": [ArtMovement.ART_DECO, ArtMovement.MODERNISM],
    "lee krasner": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "leonora carrington": [ArtMovement.SURREALISM],
    "liubov popova": [ArtMovement.CONSTRUCTIVISM],
    "louis comfort tiffany": [ArtMovement.ART_NOUVEAU],
    "man ray": [ArtMovement.DADA, ArtMovement.SURREALISM],
    "marcel duchamp": [ArtMovement.DADA, ArtMovement.CUBISM],
    "mark rothko": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "maurice de vlaminck": [ArtMovement.FAUVISM],
    "max beckmann": [ArtMovement.EXPRESSIONISM],
    "max ernst": [ArtMovement.SURREALISM, ArtMovement.DADA],
    "maximilien luce": [ArtMovement.NEO_IMPRESSIONISM],
    "mikhail vrubel": [ArtMovement.SYMBOLISM, ArtMovement.ROMANTICISM],
    "norman lewis": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "odilon redon": [ArtMovement.SYMBOLISM, ArtMovement.ROMANTICISM],
    "otto dix": [ArtMovement.EXPRESSIONISM],
    "pablo picasso": [ArtMovement.CUBISM, ArtMovement.MODERNISM],
    "paul cezanne": [ArtMovement.POST_IMPRESSIONISM],
    "paul gauguin": [ArtMovement.POST_IMPRESSIONISM],
    "paul signac": [ArtMovement.NEO_IMPRESSIONISM],
    "piet mondrian": [ArtMovement.MODERNISM],
    "raoul dufy": [ArtMovement.FAUVISM],
    "rem magritte": [ArtMovement.SURREALISM],
    "rene magritte": [ArtMovement.SURREALISM],
    "richard hamilton": [ArtMovement.POP_ART],
    "robert delaunay": [ArtMovement.CUBISM],
    "robert morris": [ArtMovement.MINIMALISM],
    "robert rauschenberg": [ArtMovement.POP_ART],
    "roy lichtenstein": [ArtMovement.POP_ART],
    "salvador dali": [ArtMovement.SURREALISM],
    "sol lewitt": [ArtMovement.MINIMALISM],
    "sophie taeuber arnold": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "sophie tauber": [ArtMovement.CUBISM],
    "sophie tauber arnold": [ArtMovement.CUBISM],
    "tamara de lempicka": [ArtMovement.ART_DECO],
    "theo van doesburg": [ArtMovement.CONSTRUCTIVISM],
    "tristan tzara": [ArtMovement.DADA],
    "umberto boccioni": [ArtMovement.FUTURISM],
    "varvara stepanova": [ArtMovement.CONSTRUCTIVISM],
    "victor horta": [ArtMovement.ART_NOUVEAU],
    "victor vasarely": [ArtMovement.OP_ART],
    "vincent van gogh": [ArtMovement.POST_IMPRESSIONISM],
    "wassily kandinsky": [ArtMovement.EXPRESSIONISM, ArtMovement.ABSTRACT_EXPRESSIONISM],
    "willem de kooning": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    "yves tanguy": [ArtMovement.SURREALISM],
    "yvone bradley": [ArtMovement.MINIMALISM],
    "yvonne rainer": [ArtMovement.POP_ART],
    "zdzislaw beksinski": [ArtMovement.SURREALISM],
    "zoltan seques": [ArtMovement.ABSTRACT_EXPRESSIONISM],
    # ── Additional Old Master attributions ─────────────────
    "antois coypel": [ArtMovement.BAROQUE, ArtMovement.ROCOCO],
    "bartolome murillo": [ArtMovement.BAROQUE],
    "carel van falens": [ArtMovement.ROMANTICISM],
    "charles andre van loo": [ArtMovement.ROCOCO],
    "charles de groux": [ArtMovement.ROMANTICISM],
    "charles francois daubigny": [ArtMovement.REALISM],
    "felix cottreau": [ArtMovement.BAROQUE],
    "francisco de zurbaran": [ArtMovement.BAROQUE],
    "gerard ter borch": [ArtMovement.BAROQUE],
    "gerrit dou": [ArtMovement.BAROQUE],
    "henri fantin latour": [ArtMovement.REALISM, ArtMovement.IMPRESSIONISM],
    "hercules pietersz seghers": [ArtMovement.BAROQUE],
    "hubert robert": [ArtMovement.BAROQUE],
    "jan van bijlert": [ArtMovement.BAROQUE],
    "johann heinrich schonfeld": [ArtMovement.BAROQUE],
    "joseph franck": [ArtMovement.BAROQUE],
    "louis remy lefebvre": [ArtMovement.NEOCLASSICISM],
    "luis de morales": [ArtMovement.MANNERISM],
    "matthias stom": [ArtMovement.BAROQUE],
    "narcisse diaz de la pena": [ArtMovement.BAROQUE, ArtMovement.ROMANTICISM],
    "octave tassaert": [ArtMovement.ROMANTICISM],
    "pieter codde": [ArtMovement.BAROQUE],
    "theodore chasseriau": [ArtMovement.ROMANTICISM],
    "theodore fourmois": [ArtMovement.ROMANTICISM],
}

_PARENTHETICAL = re.compile(r"\(([^)]*)\)")

# Museums append dates, nationalities, and roles to artist names
# ("Jackson Pollock American 1912"). These are never part of a lookup key.
_NOISE = re.compile(
    r"\b\d{3,4}\s*[-\u2013]\s*\d{0,4}\b"  # date ranges
    r"|\b\d{3,4}\b"  # single years
    r"|\b(?:american|british|english|french|italian|spanish|dutch|flemish|german|"
    r"austrian|swiss|belgian|russian|polish|danish|swedish|norwegian|mexican|"
    r"japanese|chinese|canadian|australian|scottish|welsh|irish|greek|hungarian|"
    r"czech|turkish|egyptian|iranian|indian|korean|portuguese|romanian|ukrainian)\b"
    r"|\b(?:artist|painter|printmaker|sculptor|engraver|draughtsman|draftsm)\b",
    re.IGNORECASE,
)


_PUNCT_SEPARATORS = re.compile(r"[-'\u2019\u2010]")


def _key(value: str) -> str:
    """Canonical lookup key: unaccented, lowercased, punctuation-split, collapsed.

    Museums disagree on separators: "Jean-Honoré Fragonard" and
    "Jean Honoré Fragonard" are the same artist, so hyphens and apostrophes
    must not create two different artists.
    """
    spaced = _PUNCT_SEPARATORS.sub(" ", strip_accents(value).lower())
    return " ".join(spaced.split())


def normalize_artist_name(artist_name: str) -> list[str]:
    """Produce the ordered list of lookup keys for a museum artist string.

    Museums are inconsistent: "Monet, Claude (French, 1840-1926)",
    "El Greco (Domenikos Theotokopoulos)", and "Vincent van Gogh" must all
    resolve. Variants are ordered most-specific first.
    """
    if not artist_name:
        return []

    raw = artist_name.strip()
    variants: list[str] = []

    # "El Greco (Domenikos Theotokopoulos)" → try the alias in parentheses first.
    for alias in _PARENTHETICAL.findall(raw):
        cleaned = _NOISE.sub(" ", alias).strip(" ,;")
        if cleaned:
            variants.append(cleaned)

    # "Monet, Claude (French, 1840-1926)" → try both name orderings.
    for candidate in (_PARENTHETICAL.sub(" ", raw), raw):
        cleaned = _NOISE.sub(" ", candidate).strip(" ,;")
        if not cleaned:
            continue
        variants.append(cleaned)
        if "," in cleaned:
            head, _, tail = cleaned.partition(",")
            reordered = f"{tail.strip()} {head.strip()}".strip()
            if reordered:
                variants.append(reordered)

    keys: list[str] = []
    for variant in variants:
        normalized = _key(variant)
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def lookup_artist_movements(artist_name: str) -> list[ArtMovement] | None:
    """Look up known movements for a canonical artist name.

    Returns None if the artist is not in the known mappings.
    Returns a list of movements if found (first = primary).
    """
    for key in normalize_artist_name(artist_name):
        movements = ARTIST_MOVEMENTS.get(key)
        if movements:
            return movements
    return None


def _assert_keys_are_canonical() -> None:
    """Fail loudly if a source key is not already in canonical form."""
    offenders = [key for key in ARTIST_MOVEMENTS if _key(key) != key]
    if offenders:
        raise RuntimeError(f"Non-canonical artist keys in ARTIST_MOVEMENTS: {offenders}")


_assert_keys_are_canonical()


def known_artist_count() -> int:
    """Number of artists with at least one movement association."""
    return sum(1 for movements in ARTIST_MOVEMENTS.values() if movements)
