from django import forms
from django.db import models

from .models import Material, MaterialPrice, MixIngredient, MixRatio, Project


class ProjectForm(forms.ModelForm):
    half_quantity = forms.IntegerField(label='Half blocks (H)', min_value=0, initial=0, required=False)
    full_quantity = forms.IntegerField(label='Full blocks (F)', min_value=0, initial=0, required=False)
    required_strength_mpa = forms.DecimalField(
        label='Required strength (MPa)', max_digits=6, decimal_places=2, min_value=0, required=False
    )

    class Meta:
        model = Project
        fields = ['name', 'description']
        labels = {'name': 'Project name'}
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get('half_quantity') or 0) and not (cleaned.get('full_quantity') or 0):
            raise forms.ValidationError('Enter the number of half or full blocks.')
        return cleaned

    def block_quantities(self):
        """Yield (block_type, quantity) for every block type with a quantity > 0."""
        for block_type, field in (('H', 'half_quantity'), ('F', 'full_quantity')):
            quantity = self.cleaned_data.get(field) or 0
            if quantity:
                yield block_type, quantity


class ProjectEditForm(ProjectForm):
    """Manager edit: same fields as creating. Status changes on the project page instead."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            blocks = {b.block_type: b for b in self.instance.blocks.all()}
            self.initial['half_quantity'] = blocks['H'].quantity if 'H' in blocks else 0
            self.initial['full_quantity'] = blocks['F'].quantity if 'F' in blocks else 0
            strengths = [b.required_strength_mpa for b in blocks.values() if b.required_strength_mpa is not None]
            self.initial['required_strength_mpa'] = strengths[0] if strengths else None


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ['name', 'base_unit']
        labels = {'base_unit': 'Unit (e.g. wheelbarrow, bag)'}


class MaterialEditForm(forms.ModelForm):
    """The unit is fixed once a material exists, so old prices and mixes keep their meaning."""

    class Meta:
        model = Material
        fields = ['name', 'is_active']
        labels = {'is_active': 'Available for new mixes and prices'}


class MaterialPriceForm(forms.ModelForm):
    PER_BASE_UNIT = 'base'
    PER_M3 = 'm3'

    price_unit = forms.ChoiceField(
        label='Price is for',
        choices=[
            (PER_BASE_UNIT, "One unit of the material (wheelbarrow, bag, ...)"),
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
        return cleaned

    def save(self, commit=True):
        if self.cleaned_data['price_unit'] == self.PER_M3:
            self.instance.unit = 'm3'
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
        """Sand, stone, cement first, as on the mix ratio sheet; anything else after, by name."""
        name = material.name.lower()
        rank = next((i for i, word in enumerate(('sand', 'stone', 'cement')) if word in name), 3)
        return rank, name

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
