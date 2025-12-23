# Generated migration for Google Scholar models

from django.db import migrations, models
import django.db.models.functions


class Migration(migrations.Migration):

    dependencies = [
        ('fighthealthinsurance', '0140_add_chooser_skip_model'),
    ]

    operations = [
        migrations.CreateModel(
            name='GoogleScholarArticleSummarized',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('article_id', models.TextField(blank=True)),
                ('title', models.TextField(blank=True, null=True)),
                ('publication_info', models.TextField(blank=True, null=True)),
                ('snippet', models.TextField(blank=True, null=True)),
                ('text', models.TextField(blank=True, null=True)),
                ('basic_summary', models.TextField(blank=True, null=True)),
                ('cited_by_count', models.IntegerField(blank=True, null=True)),
                ('cited_by_link', models.TextField(blank=True, null=True)),
                ('article_url', models.TextField(blank=True, null=True)),
                ('pdf_file', models.TextField(blank=True, null=True)),
                ('query', models.TextField(blank=True)),
                ('created', models.DateTimeField(db_default=django.db.models.functions.Now(), null=True)),
            ],
        ),
        migrations.CreateModel(
            name='GoogleScholarMiniArticle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('article_id', models.TextField(blank=True)),
                ('title', models.TextField(blank=True, null=True)),
                ('snippet', models.TextField(blank=True, null=True)),
                ('publication_info', models.TextField(blank=True, null=True)),
                ('cited_by_count', models.IntegerField(blank=True, null=True)),
                ('cited_by_link', models.TextField(blank=True, null=True)),
                ('article_url', models.TextField(blank=True, null=True)),
                ('pdf_file', models.TextField(blank=True, null=True)),
                ('created', models.DateTimeField(db_default=django.db.models.functions.Now(), null=True)),
            ],
        ),
        migrations.CreateModel(
            name='GoogleScholarQueryData',
            fields=[
                ('internal_id', models.AutoField(primary_key=True, serialize=False)),
                ('query', models.TextField(max_length=300)),
                ('since', models.TextField(null=True)),
                ('articles', models.TextField(null=True)),
                ('query_date', models.DateTimeField(auto_now_add=True)),
                ('created', models.DateTimeField(db_default=django.db.models.functions.Now(), null=True)),
                ('denial_id', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='fighthealthinsurance.denial')),
            ],
        ),
    ]
