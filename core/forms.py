from decimal import Decimal

from django import forms
from django.db import models

from .models import Material, MaterialPrice, MixIngredient, MixRatio, Project
from .services import mix_fits
from .templatetags.ration import recipe_rank


class ProjectForm(forms.ModelForm):
    """Project details, block counts and the one mixture used for every block."""

    full_quantity = forms.IntegerField(label='Full blocks', min_value=0, initial=0, required=False)
    half_quantity = forms.IntegerField(label='Half blocks', min_value=0, initial=0, required=False)
    mix = forms.ModelChoiceField(
        label='Mixture', queryset=MixRatio.objects.none(), widget=forms.RadioSelect,
        error_messages={'required': 'Choose a mixture.'},
    )

    class Meta:
        model = Project
        fields = ['name', 'description', 'mix']
        labels = {'name': 'Project name'}
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mixes = MixRatio.objects.filter(is_active=True)
        if self.instance.pk and self.instance.mix_id:
            # Keep a mixture that was deactivated after the project chose it.
            mixes = MixRatio.objects.filter(models.Q(is_active=True) | models.Q(pk=self.instance.mix_id))
        self.fields['mix'].queryset = mixes.order_by('name')
        if self.instance.pk and not self.is_bound:
            blocks = {b.block_type: b.quantity for b in self.instance.blocks.all()}
            self.initial['full_quantity'] = blocks.get('F', 0)
            self.initial['half_quantity'] = blocks.get('H', 0)

    def clean(self):
        cleaned = super().clean()
        types = [t for t, _ in self.block_quantities()]
        if not types:
            raise forms.ValidationError('Enter the number of full or half blocks.')
        mix = cleaned.get('mix')
        if mix and not mix_fits(mix, types):
            self.add_error('mix', f'{mix.name} is only for {mix.get_block_type_display().lower()} blocks.')
        return cleaned

    def block_quantities(self):
        """Yield (block_type, quantity) for every block type with a quantity > 0."""
        for block_type, field in (('F', 'full_quantity'), ('H', 'half_quantity')):
            quantity = self.cleaned_data.get(field) or 0
            if quantity:
                yield block_type, quantity


class ProjectPriceForm(forms.Form):
    """A manager's price for one material on one project, per tonne when the weight is known."""

    price = forms.DecimalField(min_value=Decimal('0.01'), max_digits=12, decimal_places=2)

    def __init__(self, material, *args, **kwargs):
        self.material = material
        super().__init__(*args, **kwargs)

    @property
    def per_tonne(self):
        return bool(self.material.tonnes_per_unit)

    def unit(self):
        return 't' if self.per_tonne else self.material.base_unit


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ['name', 'base_unit', 'tonnes_per_unit']
        labels = {'base_unit': 'Unit (e.g. wheelbarrow, bag)', 'tonnes_per_unit': 'Tonnes per unit'}


class MaterialEditForm(forms.ModelForm):
    """The unit is fixed once a material exists, so old prices and mixes keep their meaning."""

    class Meta:
        model = Material
        fields = ['name', 'tonnes_per_unit', 'is_active']
        labels = {'tonnes_per_unit': 'Tonnes per unit', 'is_active': 'Available for new mixes and prices'}


class MaterialPriceForm(forms.ModelForm):
    PER_BASE_UNIT = 'base'
    PER_M3 = 'm3'

    price_unit = forms.ChoiceField(
        label='Price is for',
        choices=[
            ('t', 'One tonne'),
            (PER_BASE_UNIT, 'One unit of the material (wheelbarrow, bag, ...)'),
            (PER_M3, 'One cubic metre (sand/stone: ÷ 16 wheelbarrows)'),
        ],
    )

    class Meta:
        model = MaterialPrice
        fields = ['material', 'price', 'price_unit', 'currency']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['material'].queryset = Material.objects.filter(is_active=True)

    def clean_currency(self):
        return self.cleaned_data['currency'].strip().upper()

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get('material')
        if cleaned.get('price_unit') == self.PER_M3 and material and material.base_unit != 'wheelbarrow':
            self.add_error('price_unit', 'Cubic metre prices only work for materials measured in wheelbarrows.')
        if cleaned.get('price_unit') == 't' and material and not material.tonnes_per_unit:
            self.add_error('price_unit', f'Set the tonnes per unit for {material.name} first, or price it per unit.')
        return cleaned

    def save(self, commit=True):
        if self.cleaned_data['price_unit'] in (self.PER_M3, 't'):
            self.instance.unit = self.cleaned_data['price_unit']
        else:
            self.instance.unit = self.cleaned_data['material'].base_unit
        return super().save(commit)


class MixRatioForm(forms.ModelForm):
    """A mix with one quantity box per material (sand, stone, cement) instead of picking materials."""

    class Meta:
        model = MixRatio
        fields = ['name', 'block_type', 'strength_mpa', 'blocks_per_batch', 'is_active']
        labels = {
            'block_type': 'Used for',
            'strength_mpa': 'Strength (MPa)',
            'blocks_per_batch': 'Blocks per batch',
            'is_active': 'Active',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        existing = {i.material_id: i for i in self.instance.ingredients.all()} if self.instance.pk else {}
        # Every available material, plus any unavailable one this mix already uses.
        self.materials = sorted(
            Material.objects.filter(models.Q(is_active=True) | models.Q(pk__in=existing.keys())),
            key=self.recipe_order,
        )
        for material in self.materials:
            ingredient = existing.get(material.pk)
            self.fields[self.quantity_field(material)] = forms.DecimalField(
                label=material.name, required=False, min_value=0, max_digits=12, decimal_places=3,
                initial=ingredient.quantity.normalize() if ingredient else None,
                widget=forms.NumberInput(attrs={'step': '0.5', 'inputmode': 'decimal', 'placeholder': '0'}),
            )

    @staticmethod
    def recipe_order(material):
        return recipe_rank(material)

    @staticmethod
    def quantity_field(material):
        return f'qty_{material.pk}'

    def material_rows(self):
        """(material, bound field) pairs for the template."""
        return [(m, self[self.quantity_field(m)]) for m in self.materials]

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(self.quantity_field(m)) for m in self.materials):
            raise forms.ValidationError('Enter a quantity for at least one material.')
        return cleaned

    def save(self, commit=True):
        mix = super().save(commit=commit)
        if commit:
            self.save_ingredients(mix)
        return mix

    def save_ingredients(self, mix):
        for material in self.materials:
            quantity = self.cleaned_data.get(self.quantity_field(material))
            if quantity:
                MixIngredient.objects.update_or_create(
                    mix=mix, material=material, defaults={'quantity': quantity, 'unit': material.base_unit},
                )
            else:
                MixIngredient.objects.filter(mix=mix, material=material).delete()
