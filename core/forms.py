from decimal import Decimal

from django import forms
from django.db import models

from .models import Material, MaterialPrice, MixIngredient, MixRatio, Project
from .services import mix_fits
from .templatetags.ration import recipe_rank


class ProjectForm(forms.ModelForm):
    """Project details, block counts and the one mixture used for every block."""

    full_quantity = forms.IntegerField(label='Full blocks', min_value=0, required=False)
    half_quantity = forms.IntegerField(label='Half blocks', min_value=0, required=False)
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
            self.initial['full_quantity'] = blocks.get('F')
            self.initial['half_quantity'] = blocks.get('H')

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


UNIT_CHOICES = [
    ('wheelbarrow', 'Wheelbarrow (sand, stone)'),
    ('bag', 'Bag (cement)'),
    ('tonne', 'Tonne (measured by weight)'),
]
KG_PER_TONNE = Decimal('1000')


class MaterialWeightMixin:
    """Weight is entered in kg per unit (50 for a cement bag) and stored as tonnes per unit."""

    def add_weight_field(self, unit, tonnes):
        self.fields['weight_kg'] = forms.DecimalField(
            label='Weight of one unit (kg)', required=False, min_value=Decimal('0.001'),
            max_digits=9, decimal_places=3,
            initial=f'{(tonnes * KG_PER_TONNE).normalize():f}' if tonnes else None,
            help_text='A cement bag is 50 kg; a wheelbarrow of sand is about 100 kg, of 13 mm stone about 90 kg.',
            widget=forms.NumberInput(attrs={'step': '0.1', 'inputmode': 'decimal', 'placeholder': 'e.g. 50'}),
        )
        # Needed unless the material is bought by the tonne; the form checks that itself.
        self.fields['weight_kg'].hide_optional = True

    def tonnes_per_unit(self, unit):
        if unit == 'tonne':
            return Decimal('1')
        kg = self.cleaned_data.get('weight_kg')
        return (kg / KG_PER_TONNE).quantize(Decimal('0.0001')) if kg else None

    def check_weight(self, unit):
        if unit and unit != 'tonne' and not self.cleaned_data.get('weight_kg'):
            self.add_error('weight_kg', f'Enter how many kg one {unit} weighs, so quantities and prices show in tonnes.')


class MaterialForm(MaterialWeightMixin, forms.ModelForm):
    base_unit = forms.ChoiceField(label='Bought and measured by', choices=UNIT_CHOICES, widget=forms.RadioSelect)

    class Meta:
        model = Material
        fields = ['name', 'base_unit']
        labels = {'name': 'Material name'}
        widgets = {'name': forms.TextInput(attrs={'placeholder': 'e.g. Sand'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_weight_field(None, None)

    def clean(self):
        cleaned = super().clean()
        self.check_weight(cleaned.get('base_unit'))
        return cleaned

    def save(self, commit=True):
        self.instance.tonnes_per_unit = self.tonnes_per_unit(self.cleaned_data['base_unit'])
        return super().save(commit)


class MaterialEditForm(MaterialWeightMixin, forms.ModelForm):
    """The unit is fixed once a material exists, so old prices and mixes keep their meaning."""

    class Meta:
        model = Material
        fields = ['name', 'is_active']
        labels = {'is_active': 'Available for new mixes and prices'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.base_unit != 'tonne':
            self.add_weight_field(self.instance.base_unit, self.instance.tonnes_per_unit)
            self.fields['weight_kg'].label = f'Weight of one {self.instance.base_unit} (kg)'

    def clean(self):
        cleaned = super().clean()
        self.check_weight(self.instance.base_unit)
        return cleaned

    def save(self, commit=True):
        self.instance.tonnes_per_unit = self.tonnes_per_unit(self.instance.base_unit)
        return super().save(commit)


def price_bases(material):
    """What a price for this material can be entered per: [(value, label)], most useful first."""
    bases = []
    if material.tonnes_per_unit and material.base_unit != 'tonne':
        bases.append(('t', 'tonne'))
    bases.append(('base', material.base_unit))
    if material.base_unit == 'wheelbarrow':
        bases.append(('m3', 'm³ (16 wheelbarrows)'))
    return bases


class MaterialPriceForm(forms.ModelForm):
    """A default price. Which units it can be entered in depends on the material."""

    PER_BASE_UNIT = 'base'
    PER_M3 = 'm3'

    price_unit = forms.ChoiceField(
        label='Price per',
        choices=[('t', 'tonne'), (PER_BASE_UNIT, 'unit'), (PER_M3, 'm³')],
    )

    class Meta:
        model = MaterialPrice
        fields = ['material', 'price_unit', 'price', 'currency']
        labels = {'price': 'Price'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['material'].queryset = Material.objects.filter(is_active=True).order_by('name')
        self.fields['material'].empty_label = 'Choose a material'
        self.fields['price'].widget.attrs.update({'step': '0.01', 'inputmode': 'decimal', 'placeholder': '0.00'})
        self.fields['currency'].widget.attrs.update({'list': 'currency-options', 'autocomplete': 'off'})

    def clean_currency(self):
        return self.cleaned_data['currency'].strip().upper()

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get('material')
        basis = cleaned.get('price_unit')
        if material and basis and basis not in dict(price_bases(material)):
            allowed = ', '.join(label.split(' ')[0] for _, label in price_bases(material))
            self.add_error('price_unit', f'{material.name} can be priced per {allowed}.')
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
