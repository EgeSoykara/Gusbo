from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("Myapp", "0021_customerprofile_phone_verified_at_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="customerprofile",
            name="phone_verified_at",
        ),
        migrations.DeleteModel(
            name="CreditTransaction",
        ),
        migrations.DeleteModel(
            name="PhoneVerificationCode",
        ),
        migrations.DeleteModel(
            name="ProviderWallet",
        ),
    ]
