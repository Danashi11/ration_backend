import math
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Max

from .models import EstimateMaterial, Material, MaterialPrice, MixBlockType, MixRatio, ProjectEstimate

# "Divide the cost of one cubic metre sand/stone by 16 to get 1 wheelbarrow price."
WHEELBARROWS_PER_M3 = Decimal('16')
CENTS = Decimal('0.01')


def price_per_base_unit(price):
    """Price of one base unit (wheelbarrow, bag) of the material, whatever unit it was entered in."""
    material = price.material
    if price.unit == 'm3' and material.base_unit == 'wheelbarrow':
        return price.price / WHEELBARROWS_PER_M3
    if price.unit == 't' and material.tonnes_per_unit:
        return price.price * material.tonnes_per_unit
    return price.price


def price_per_tonne(material, unit_price):
    if unit_price is None or not material.tonnes_per_unit:
        return None
    return (unit_price / material.tonnes_per_unit).quantize(CENTS)


def latest_approved_prices(project=None):
    """Map material_id -> the MaterialPrice to use (business rule 1).

    Approved default prices (no project) apply everywhere. A project's own prices
    replace them for that project; the accountant checks those when approving it.
    """
    prices = {}
    defaults = (
        MaterialPrice.objects.filter(status=MaterialPrice.Status.APPROVED, project__isnull=True)
        .select_related('material')
        .order_by('material_id', F('reviewed_at').desc(nulls_last=True), '-created_at')
    )
    for price in defaults:
        prices.setdefault(price.material_id, price)
    if project is not None:
        overrides = {}
        own = (
            MaterialPrice.objects.filter(status=MaterialPrice.Status.APPROVED, project=project)
            .select_related('material')
            .order_by('material_id', '-created_at')
        )
        for price in own:
            overrides.setdefault(price.material_id, price)
        prices.update(overrides)
    return prices


def mix_fits(mix, block_types):
    """One mixture makes every block in a project, so it must suit all of its block types."""
    block_types = set(block_types)
    return mix.block_type == MixBlockType.BOTH or (bool(block_types) and block_types == {mix.block_type})


def cost_mix(mix, quantities, prices):
    """Materials and cost of making the given blocks ({'F': 1200, 'H': 300}) with one mixture."""
    ingredients = list(mix.ingredients.all())
    lines = []
    needed = {}  # material_id -> base-unit quantity
    for block_type in ('F', 'H'):  # full before half
        quantity = quantities.get(block_type) or 0
        if not quantity:
            continue
        batches = math.ceil(quantity / mix.blocks_per_batch)
        line_total = Decimal('0')
        line_priced = True
        for ingredient in ingredients:
            amount = ingredient.quantity * batches
            needed[ingredient.material_id] = needed.get(ingredient.material_id, Decimal('0')) + amount
            price = prices.get(ingredient.material_id)
            if price is None:
                line_priced = False
            else:
                line_total += (amount * price_per_base_unit(price)).quantize(CENTS)
        lines.append({
            'block_type': block_type,
            'quantity': quantity,
            'batches': batches,
            'blocks_produced': batches * mix.blocks_per_batch,
            'total': line_total if line_priced and ingredients else None,
        })

    materials = []
    missing = []
    total = Decimal('0')
    for ingredient in ingredients:
        material = ingredient.material
        amount = needed.get(material.pk, Decimal('0'))
        price = prices.get(material.pk)
        unit_price = price_per_base_unit(price) if price else None
        subtotal = (amount * unit_price).quantize(CENTS) if price else None
        if price is None:
            missing.append(material.name)
        else:
            total += subtotal
        materials.append({
            'material': material,
            'per_batch': ingredient.quantity,
            'quantity': amount,
            'unit': ingredient.unit,
            'tonnes': material.to_tonnes(amount),
            'unit_price': unit_price.quantize(CENTS) if unit_price is not None else None,
            'price_per_tonne': price_per_tonne(material, unit_price),
            'currency': price.currency.upper() if price else None,
            'project_price': price if price and price.project_id else None,
            'subtotal': subtotal,
        })

    blocks = sum(line['quantity'] for line in lines)
    currencies = sorted({m['currency'] for m in materials if m['currency']})
    complete = bool(ingredients) and bool(lines) and not missing and len(currencies) <= 1
    return {
        'mix': mix,
        'lines': lines,
        'materials': materials,
        'missing': missing,
        'blocks': blocks,
        'total': total if complete else None,
        'currency': currencies[0] if len(currencies) == 1 else None,
        'mixed_currencies': len(currencies) > 1,
        'cost_per_block': (total / blocks).quantize(CENTS) if complete and blocks else None,
    }


def project_quantities(project):
    return {block.block_type: block.quantity for block in project.blocks.all()}


def estimate_project(project):
    """The project's materials and total with its chosen mixture and its prices."""
    prices = latest_approved_prices(project)
    quantities = project_quantities(project)
    mix = project.mix
    if mix is None:
        return {
            'mix': None, 'lines': [], 'materials': [], 'missing': [], 'prices': prices,
            'blocks': sum(quantities.values()), 'total': None, 'currency': None,
            'mixed_currencies': False, 'cost_per_block': None,
        }
    mix = MixRatio.objects.prefetch_related('ingredients__material').get(pk=mix.pk)
    estimate = cost_mix(mix, quantities, prices)
    estimate['prices'] = prices
    return estimate


def pricing_data(project=None):
    """Mixtures and current prices for the live totals on the create and edit pages."""
    prices = latest_approved_prices(project)
    materials = []
    for material in Material.objects.order_by('name'):
        price = prices.get(material.pk)
        unit_price = price_per_base_unit(price) if price else None
        materials.append({
            'id': material.pk,
            'name': material.name,
            'unit': material.base_unit,
            'tonnes_per_unit': float(material.tonnes_per_unit) if material.tonnes_per_unit else None,
            'unit_price': float(unit_price) if unit_price is not None else None,
            'currency': price.currency.upper() if price else None,
        })
    mixes = [
        {
            'id': mix.pk,
            'block_type': mix.block_type,
            'blocks_per_batch': mix.blocks_per_batch,
            'ingredients': [
                {'material': i.material_id, 'quantity': float(i.quantity)} for i in mix.ingredients.all()
            ],
        }
        for mix in MixRatio.objects.filter(is_active=True).prefetch_related('ingredients')
    ]
    return {'materials': materials, 'mixes': mixes}


def save_estimate(project, user, estimate=None):
    """Store the project's current costs as a new estimate version (business rule 2).

    The prices are copied into the estimate, so later price changes don't alter it.
    Returns None if the project total isn't complete.
    """
    estimate = estimate or estimate_project(project)
    if estimate['total'] is None:
        return None

    mix = estimate['mix']
    labels = dict(project.blocks.model._meta.get_field('block_type').choices)
    ingredients = [
        {'material': ingredient.material.name, 'quantity': str(ingredient.quantity), 'unit': ingredient.unit}
        for ingredient in mix.ingredients.all()
    ]
    breakdown = [
        {
            'block_type': line['block_type'],
            'block_label': labels[line['block_type']],
            'quantity': line['quantity'],
            'mix': mix.name,
            'strength_mpa': str(mix.strength_mpa) if mix.strength_mpa is not None else None,
            'batches': line['batches'],
            'blocks_produced': line['blocks_produced'],
            'cost_per_block': str((line['total'] / line['blocks_produced']).quantize(CENTS)),
            'total': str(line['total']),
            # One batch of the mixture as it was when saved.
            'ingredients': ingredients,
        }
        for line in estimate['lines']
    ]

    with transaction.atomic():
        last = project.estimates.aggregate(v=Max('version'))['v'] or 0
        saved = ProjectEstimate.objects.create(
            project=project,
            version=last + 1,
            total_material_cost=estimate['total'],
            currency=estimate['currency'] or 'ZAR',
            calculated_by=user,
            breakdown=breakdown,
        )
        EstimateMaterial.objects.bulk_create([
            EstimateMaterial(
                estimate=saved, material=row['material'], quantity_required=row['quantity'], unit=row['unit'],
                unit_price_snapshot=row['unit_price'], subtotal=row['subtotal'],
            )
            for row in sorted(estimate['materials'], key=lambda r: r['material'].name)
        ])
    return saved
