from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_improve_provider_account_config_help"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="ChargeableEvent",
            new_name="InboundPaymentEvent",
        ),
        migrations.RenameModel(
            old_name="PaymentMethodToken",
            new_name="InboundPaymentMethodToken",
        ),
        migrations.RenameModel(
            old_name="PaymentIntent",
            new_name="InboundPayment",
        ),
        migrations.RenameModel(
            old_name="Transaction",
            new_name="InboundPaymentTransaction",
        ),
        migrations.RenameModel(
            old_name="TransactionStateLog",
            new_name="InboundPaymentTransactionStateLog",
        ),
        migrations.RenameModel(
            old_name="ProviderCallbackLog",
            new_name="InboundPaymentCallbackLog",
        ),
        migrations.RenameModel(
            old_name="ReconciliationRecord",
            new_name="InboundPaymentReconciliation",
        ),
    ]
