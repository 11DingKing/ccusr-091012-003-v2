from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('cases', '0001_initial'),
        ('warehouse', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='goods',
            name='case',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='goods_set',
                to='cases.case',
                verbose_name='所属案件',
            ),
        ),
    ]
