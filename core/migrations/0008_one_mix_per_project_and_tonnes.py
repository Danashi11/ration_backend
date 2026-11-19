from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models

# Starting weights per base unit (editable per material). 1 m³ = 16 wheelbarrows:
# sand ~1.6 t/m³, 13 mm stone ~1.45 t/m³, cement bags are 50 kg.
STARTING_TONNES = [
    ('sand', Decimal('0.1000')),
    ('stone', Decimal('0.0900')),
    ('cement', Decimal('0.0500')),
]


def forward(apps, schema_editor):
    Project = apps.get_model('core', 'Project')
    ProjectBlock = apps.get_model('core', 'ProjectBlock')
    Material = apps.get_model('core', 'Material')

    # One mixture per project: keep the one chosen for full blocks, else any chosen one.
    for project in Project.objects.all():
        blocks = ProjectBlock.objects.filter(project=project, mix__isnull=False).order_by('block_type')
        block = blocks.filter(block_type='F').first() or blocks.first()
        if block:
            project.mix_id = block.mix_id
            project.save(update_fields=['mix'])

    for material in Material.objects.filter(tonnes_per_unit__isnull=True):
        name = material.name.lower()
        for word, tonnes in STARTING_TONNES:
            if word in name:
                material.tonnes_per_unit = tonnes
                material.save(update_fields=['tonnes_per_unit'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_project_status_flow'),
    ]

    operations = [
        migrations.AddField(
            model_name='material',
            name='tonnes_per_unit',
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name='project',
            name='mix',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='projects', to='core.mixratio'),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='projectblock',
            name='mix',
        ),
        migrations.RemoveField(
            model_name='projectblock',
            name='required_strength_mpa',
        ),
    ]
