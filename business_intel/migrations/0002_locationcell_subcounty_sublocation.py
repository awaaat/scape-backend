# Generated for: adding real KNBS sub-county / sub-location fields to
# BusinessLocationCell. Pair with the models.py patch in
# models_py_PATCH_INSTRUCTIONS.md before running `manage.py makemigrations`
# to confirm Django agrees no further changes are pending (it should
# report "No changes detected" if this migration was written correctly
# against your actual models.py).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business_intel', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='businesslocationcell',
            name='subcounty',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text="Real KNBS sub-county name, e.g. 'Ruiru'. Distinct from `ward`.",
                max_length=100,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='businesslocationcell',
            name='sublocation',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    "Real KNBS sub-location name -- the finest administrative unit KNBS "
                    "publishes population figures for (an EA 'may be a village, group of "
                    "villages or part of a village', per KNBS's own definition). Population "
                    "data at this level is not yet available for most of the country; see "
                    "business_intel.data.kenya_sublocations_2019_census (Mombasa only, as of "
                    "this writing) and RealAdminHierarchyProvider's docstring."
                ),
                max_length=100,
            ),
            preserve_default=False,
        ),
        migrations.AddIndex(
            model_name='businesslocationcell',
            index=models.Index(fields=['county', 'subcounty'], name='business_in_county_subco_idx'),
        ),
    ]
