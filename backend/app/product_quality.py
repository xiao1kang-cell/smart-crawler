"""Shared product-quality filters."""
from __future__ import annotations

from sqlalchemy import and_, func, not_, or_

from .models import Product


_COSTWAY_CATEGORY_BASENAMES = {
    "animalerie",
    "appliances",
    "arredamento",
    "articoli-per-animali",
    "baby-kind",
    "ba-o",
    "badezimmer",
    "bagno",
    "bambini-e-neonati",
    "bebes-et-tout-petits",
    "baby-kids",
    "bath",
    "canopies-gazebos",
    "bebes-et-tout-petits",
    "cuisine-et-salle-a-manger",
    "cocina",
    "decor",
    "decoracion",
    "decorations",
    "deportes-y-aire-libre",
    "dekoration",
    "decorazione",
    "electromenagers",
    "electrodomesticos",
    "elettrodomestici",
    "garten",
    "giochi-e-giocattoli",
    "furniture",
    "haushaltsgerate",
    "haustierbedarf",
    "health-beauty",
    "jardin-et-pelouses",
    "jardin",
    "jeux-et-jouets",
    "juguetes-y-aficiones",
    "kitchen",
    "kuche",
    "infantil",
    "mascotas",
    "meubles",
    "mobel",
    "muebles",
    "muebles-exteriores",
    "oficina",
    "others",
    "outdoor-e-giardino",
    "outdoor",
    "pets",
    "pflege-kosmetik",
    "sala-da-pranzo-e-cucina",
    "salle-de-bain",
    "sante-et-beaute",
    "salud-y-belleza",
    "salute-e-bellezza",
    "spielzeuge-hobbys",
    "sports",
    "toys-hobbies",
    "sport-e-tempo-libero",
    "sport-freizeit",
    "sports-et-plein-air",
    "terraza-y-jardin",
}

_COSTWAY_NON_PRODUCT_TOKENS = (
    "affiliate",
    "agrupados",
    "aw-reward-points",
    "back-to-school",
    "black",
    "bf-",
    "bfdealstoroyalusers",
    "carbono",
    "christmas",
    "climate-action",
    "ceshi",
    "colorfulautumn",
    "copa",
    "coupon",
    "costway-aniversario",
    "costway-home",
    "costwayday",
    "cyber",
    "deal",
    "error",
    "descuento",
    "diadel",
    "dia-del",
    "diadesanvalentin",
    "disfrutadelairelibre",
    "dropship",
    "earth",
    "ecodiseno",
    "flash",
    "freetrials",
    "garden-list",
    "get-time",
    "happy-womens-day",
    "holiday",
    "ip-security",
    "juguetes-infantiles",
    "kids",
    "kitchen",
    "labor-day",
    "liquidacion",
    "list",
    "load",
    "location-working-hours",
    "loyalty",
    "loyalty-cashback",
    "lxy",
    "m-",
    "mas-vendidos",
    "mega-semana",
    "memory-of-love",
    "milestone",
    "month",
    "monthly",
    "myrewardszone",
    "new-arrival",
    "newin",
    "novedad",
    "oferta",
    "pascua",
    "primavera",
    "recomendado",
    "regalo",
    "rosa",
    "offer",
    "outlet",
    "payment",
    "point",
    "policy",
    "programa-de-afiliados",
    "rebajas",
    "return",
    "shipping",
    "shipments",
    "singleday",
    "whatsappvip",
    "whattobuy",
    "weee-policy",
    "welcome-2022",
    "site-map",
    "subscribe",
    "summer",
    "test",
    "prueba",
    "fete",
    "fin-de-ano",
    "feliznavidad",
    "nationalday",
    "newyear",
    "paques",
    "pasqua",
    "carnaval",
    "badezimmermobel",
    "bestseller",
    "dining-room",
    "fitness-room",
    "frische-auswahl",
    "geschenke",
    "kategorie",
    "kategorie2023",
    "kinderzimmermobel",
    "living-room",
    "nationalfeiertag",
    "recommended",
    "recommended-may-like",
    "room",
    "sns",
    "wohnzimmermobel",
    "winter-sale",
    "our-guarantee",
    "reward",
    "sale",
    "school",
    "scenario",
    "scenarios",
    "outdoor-furniture",
    "appliancesale",
    "autumnsale",
    "freetrials",
    "error",
    "top-",
    "track-your-order",
    "ventadeverano",
    "vuelta",
    "vuletaalcole",
    "weekly",
    "wholesale",
    "www.costway",
    "why-costway",
)

_SALABLE_STATUSES = {
    "",
    "active",
    "available",
    "in_stock",
    "instock",
    "on_sale",
}


def product_quality_filter():
    """Return a SQLAlchemy predicate for rows that should count as products."""

    return and_(
        not_(costway_non_product_filter()),
        not_(non_physical_product_filter()),
    )


def salable_product_filter():
    """Return rows where commerce fields such as price/image should be complete."""

    return and_(
        product_quality_filter(),
        func.lower(func.trim(func.coalesce(Product.status, ""))).in_(
            tuple(sorted(_SALABLE_STATUSES))
        ),
    )


def is_salable_product_status(status: str | None) -> bool:
    return (status or "").strip().lower() in _SALABLE_STATUSES


def non_physical_product_filter():
    site = func.lower(func.coalesce(Product.site, ""))
    url = func.lower(func.coalesce(Product.product_url, ""))
    sku = func.lower(func.coalesce(Product.sku, ""))
    title = func.lower(func.coalesce(Product.title, ""))
    category = func.lower(func.coalesce(Product.category_path, ""))
    fields = (site, url, sku, title, category)
    gift_card = or_(*(field.like(pattern) for field in fields for pattern in (
        "%gift card%",
        "%gift cards%",
        "%carte cadeau%",
        "%geschenkkarte%",
        "%gift-card%",
    )))
    return or_(
        gift_card,
        or_(*(field.like("%retail delivery fee%") for field in fields)),
    )


def costway_non_product_filter():
    site = func.lower(func.coalesce(Product.site, ""))
    url = func.lower(func.coalesce(Product.product_url, ""))
    sku = func.lower(func.coalesce(Product.sku, ""))
    return and_(
        site.like("costway_%"),
        or_(
            sku.in_(tuple(_COSTWAY_CATEGORY_BASENAMES)),
            and_(
                not_(url.like("%.html%")),
                or_(*(url.like(f"%{token}%") for token in _COSTWAY_NON_PRODUCT_TOKENS)),
            ),
            or_(
                url.like("%/test%.html%"),
                url.like("%-test%.html%"),
                url.like("%/ceshi%.html%"),
                url.like("%-ceshi%.html%"),
            ),
        ),
    )


def looks_like_costway_non_product(url: str | None, sku: str | None = None) -> bool:
    text = (url or "").strip().lower()
    if not text:
        return False
    basename = text.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    stem = basename[:-5] if basename.endswith(".html") else basename
    if stem in _COSTWAY_CATEGORY_BASENAMES:
        return True
    sku_text = (sku or "").strip().lower()
    if ".html" not in text and sku_text in _COSTWAY_CATEGORY_BASENAMES:
        return True
    if basename.endswith(".html") and (
        stem.startswith(("test", "ceshi")) or "-test" in stem or "-ceshi" in stem
    ):
        return True
    return ".html" not in text and any(token in text for token in _COSTWAY_NON_PRODUCT_TOKENS)
