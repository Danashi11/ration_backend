from decimal import Decimal

from django import template

register = template.Library()

TONES = ['sand', 'stone', 'cement']
EXTRA_TONES = ['extra-1', 'extra-2', 'extra-3']


@register.filter
def material_tone(material):
    """CSS tone for a material, so sand/stone/cement keep the same colour everywhere."""
    name = material.name.lower()
    if 'sand' in name:
        return 'sand'
    if 'stone' in name or 'gravel' in name:
        return 'stone'
    if 'cement' in name:
        return 'cement'
    return EXTRA_TONES[material.pk % len(EXTRA_TONES)]


def recipe_rank(material):
    """Sand, stone, cement first, as on the mix ratio sheet; anything else after, by name."""
    name = material.name.lower()
    rank = next((i for i, word in enumerate(TONES) if word in name), len(TONES))
    return rank, name


@register.filter
def in_recipe_order(rows):
    """Sort ingredients or material rows (anything with .material or ['material']) sand, stone, cement."""
    def material_of(row):
        return row['material'] if isinstance(row, dict) else row.material
    return sorted(rows, key=lambda row: recipe_rank(material_of(row)))


@register.simple_tag
def recipe_segments(mix):
    """Ingredients of a mix with their share of the batch in measures (wheelbarrows/pockets)."""
    ingredients = sorted(mix.ingredients.all(), key=lambda i: recipe_rank(i.material))
    total = sum((i.quantity for i in ingredients), Decimal('0'))
    if not total:
        return []
    return [
        {
            'material': i.material,
            'quantity': i.quantity,
            'unit': i.unit,
            'percent': f'{i.quantity / total * 100:.2f}',
            'tone': material_tone(i.material),
        }
        for i in ingredients
    ]


@register.filter
def decimal(value):
    """Turn a saved number string (from an estimate snapshot) back into a Decimal."""
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


@register.filter
def money(value, currency='ZAR'):
    if value is None:
        return ''
    currency = (currency or 'ZAR').upper()
    symbol = 'R' if currency == 'ZAR' else currency
    return f'{symbol} {value:,.2f}'


@register.filter
def unit_label(unit):
    return {'m3': 'm³', 't': 'tonne'}.get(unit, unit)


@register.filter
def measure(quantity, unit):
    """'3 wheelbarrows', '1 bag', '2.5 wheelbarrows'."""
    text = f'{quantity.normalize():f}' if isinstance(quantity, Decimal) else str(quantity)
    if quantity != 1 and unit and not unit.endswith('s'):
        unit = f'{unit}s'
    return f'{text} {unit}'


@register.filter
def tonnes(value):
    """'8.75 t' from a Decimal number of tonnes."""
    if value is None:
        return ''
    return f'{value:,.2f} t'


@register.filter
def tonnes_of(material, quantity):
    """Tonnes for a quantity in the material's own unit, or None when its weight isn't set."""
    return material.to_tonnes(quantity) if quantity is not None else None


@register.filter
def per_tonne_of(material, unit_price):
    """Price per tonne from a price per unit, or None when the material's weight isn't set."""
    if unit_price is None or not material.tonnes_per_unit:
        return None
    return (unit_price / material.tonnes_per_unit).quantize(Decimal('0.01'))


@register.filter
def kg(tonnes):
    """'50' from 0.05 tonnes."""
    if tonnes is None:
        return ''
    return f'{(tonnes * 1000).normalize():f}'
