import json
import math

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import validate_slug
from django.db import IntegrityError, transaction

from core.models import (
    InboundPaymentEvent,
    InboundPaymentEventProviderConfig,
    OutboundTransferEvent,
    OutboundTransferEventProviderConfig,
    Provider,
    ProviderAccount,
    ProviderAccountConfig,
    ProviderConfigDefinition,
    System,
)


class ProviderConfigurationService:
    EVENT_TYPE_PAYMENT = "payment"
    EVENT_TYPE_TRANSFER = "transfer"
    EVENT_TYPES = (EVENT_TYPE_PAYMENT, EVENT_TYPE_TRANSFER)

    @classmethod
    def list_providers(cls, payment_method_type: str | None = None) -> list[dict]:
        providers = Provider.objects.filter(is_active=True).prefetch_related("payment_method_types")
        if payment_method_type:
            providers = providers.filter(
                payment_method_types__code=payment_method_type.strip().upper(),
                payment_method_types__is_active=True,
            )
        return [cls._provider_data(provider) for provider in providers.order_by("name")]

    @classmethod
    def get_provider(cls, provider_slug: str) -> dict:
        try:
            provider = Provider.objects.prefetch_related(
                "payment_method_types",
                "config_definitions",
            ).get(slug=provider_slug, is_active=True)
        except Provider.DoesNotExist as exc:
            raise ObjectDoesNotExist(f"Active provider '{provider_slug}' not found.") from exc
        return cls._provider_data(provider, include_definitions=True)

    @classmethod
    def list_organization_overrides(
        cls,
        *,
        system: System,
        organization_remote_code: str,
        event_type: str | None = None,
        event_slug: str | None = None,
    ) -> list[dict]:
        remote_code = cls._organization_remote_code(organization_remote_code)
        event_type = cls._event_type(event_type, required=False)
        rows = []
        if event_type in (None, cls.EVENT_TYPE_PAYMENT):
            configs = InboundPaymentEventProviderConfig.objects.filter(
                inbound_payment_event__system=system,
                organization_remote_code=remote_code,
            ).select_related("inbound_payment_event", "provider", "provider_account")
            if event_slug:
                configs = configs.filter(inbound_payment_event__slug=event_slug)
            rows.extend(cls._override_data(config, cls.EVENT_TYPE_PAYMENT) for config in configs)
        if event_type in (None, cls.EVENT_TYPE_TRANSFER):
            configs = OutboundTransferEventProviderConfig.objects.filter(
                outbound_transfer_event__system=system,
                organization_remote_code=remote_code,
            ).select_related("outbound_transfer_event", "provider", "provider_account")
            if event_slug:
                configs = configs.filter(outbound_transfer_event__slug=event_slug)
            rows.extend(cls._override_data(config, cls.EVENT_TYPE_TRANSFER) for config in configs)
        return sorted(rows, key=lambda row: (row["event_type"], row["event"]["slug"]))

    @classmethod
    def list_organization_provider_configs(
        cls,
        *,
        system: System,
        organization_remote_code: str,
    ) -> list[dict]:
        remote_code = cls._organization_remote_code(organization_remote_code)
        account_ids = set(
            InboundPaymentEventProviderConfig.objects.filter(
                inbound_payment_event__system=system,
                organization_remote_code=remote_code,
            ).values_list("provider_account_id", flat=True)
        )
        account_ids.update(
            OutboundTransferEventProviderConfig.objects.filter(
                outbound_transfer_event__system=system,
                organization_remote_code=remote_code,
            ).values_list("provider_account_id", flat=True)
        )
        accounts = (
            ProviderAccount.objects.filter(
                id__in=account_ids,
                organization_remote_code=remote_code,
            )
            .select_related("provider")
            .prefetch_related("config_entries", "provider__config_definitions")
            .order_by("provider__name", "name")
        )
        return [cls._provider_account_data(account) for account in accounts]

    @classmethod
    def get_organization_provider_config(
        cls,
        *,
        system: System,
        config_id: str,
        organization_remote_code: str,
    ) -> dict:
        remote_code = cls._organization_remote_code(organization_remote_code)
        is_linked = (
            InboundPaymentEventProviderConfig.objects.filter(
                inbound_payment_event__system=system,
                organization_remote_code=remote_code,
                provider_account_id=config_id,
            ).exists()
            or OutboundTransferEventProviderConfig.objects.filter(
                outbound_transfer_event__system=system,
                organization_remote_code=remote_code,
                provider_account_id=config_id,
            ).exists()
        )
        if not is_linked:
            raise ObjectDoesNotExist("Organization provider config not found.")
        try:
            account = (
                ProviderAccount.objects.select_related("provider")
                .prefetch_related("config_entries", "provider__config_definitions")
                .get(id=config_id, organization_remote_code=remote_code)
            )
        except (ProviderAccount.DoesNotExist, ValueError) as exc:
            raise ObjectDoesNotExist("Organization provider config not found.") from exc
        return cls._provider_account_data(account)

    @classmethod
    def create_organization_override(
        cls,
        *,
        system: System,
        event_slug: str,
        organization_remote_code: str,
        provider_slug: str,
        configs: dict,
        event_type: str | None = None,
    ) -> dict:
        remote_code = cls._organization_remote_code(organization_remote_code)
        if not isinstance(configs, dict):
            raise ValidationError("configs must be a JSON object.")
        event, resolved_event_type = cls._resolve_event(
            system=system,
            event_slug=event_slug,
            event_type=event_type,
        )
        try:
            provider = Provider.objects.prefetch_related("config_definitions").get(
                slug=provider_slug,
                is_active=True,
            )
        except Provider.DoesNotExist as exc:
            raise ObjectDoesNotExist(f"Active provider '{provider_slug}' not found.") from exc

        if (
            resolved_event_type == cls.EVENT_TYPE_TRANSFER
            and not provider.payment_method_types.filter(
                id=event.payment_method_type_id,
                is_active=True,
            ).exists()
        ):
            raise ValidationError(
                f"Provider '{provider.slug}' does not support {event.payment_method_type.code}."
            )

        config_model = (
            InboundPaymentEventProviderConfig
            if resolved_event_type == cls.EVENT_TYPE_PAYMENT
            else OutboundTransferEventProviderConfig
        )
        event_field = (
            "inbound_payment_event"
            if resolved_event_type == cls.EVENT_TYPE_PAYMENT
            else "outbound_transfer_event"
        )
        if config_model.objects.filter(
            **{event_field: event},
            organization_remote_code=remote_code,
        ).exists():
            raise ValidationError(
                f"Organization '{remote_code}' already has a provider override for event "
                f"'{event.slug}'."
            )

        serialized_configs = cls._validate_configs(provider, configs)
        global_config = (
            config_model.objects.select_related("provider_account")
            .filter(
                **{event_field: event},
                organization_remote_code__isnull=True,
                is_active=True,
            )
            .first()
        )
        if global_config is None:
            raise ValidationError(
                f"Event '{event.slug}' must have an active global provider configuration first."
            )
        base_account = global_config.provider_account

        try:
            with transaction.atomic():
                account = ProviderAccount.objects.create(
                    provider=provider,
                    name=f"{remote_code} {provider.name} {event.slug}"[:255],
                    organization_remote_code=remote_code,
                    environment=base_account.environment,
                    balance_tracking_enabled=base_account.balance_tracking_enabled,
                    outbound_transfer_debit_account=(
                        base_account.outbound_transfer_debit_account
                        if resolved_event_type == cls.EVENT_TYPE_TRANSFER
                        else None
                    ),
                    outbound_transfer_credit_account=(
                        base_account.outbound_transfer_credit_account
                        if resolved_event_type == cls.EVENT_TYPE_TRANSFER
                        else None
                    ),
                    is_active=True,
                )
                ProviderAccountConfig.objects.bulk_create(
                    [
                        ProviderAccountConfig(
                            provider_account=account,
                            key=definition.key,
                            value=value,
                            value_type=definition.value_type,
                        )
                        for definition, value in serialized_configs
                    ]
                )
                override = config_model(
                    **{event_field: event},
                    organization_remote_code=remote_code,
                    provider=provider,
                    provider_account=account,
                    is_active=True,
                )
                override.full_clean()
                override.save()
        except IntegrityError as exc:
            raise ValidationError("The organization provider override already exists.") from exc

        account = (
            ProviderAccount.objects.select_related("provider")
            .prefetch_related(
                "config_entries",
                "provider__config_definitions",
            )
            .get(id=account.id)
        )
        return {
            "override": cls._override_data(override, resolved_event_type),
            "provider_config": cls._provider_account_data(account),
        }

    @staticmethod
    def _provider_data(provider: Provider, *, include_definitions: bool = False) -> dict:
        data = {
            "id": str(provider.id),
            "name": provider.name,
            "slug": provider.slug,
            "is_active": provider.is_active,
            "payment_method_types": [
                {"code": item.code, "name": item.name}
                for item in provider.payment_method_types.all()
                if item.is_active
            ],
        }
        if include_definitions:
            data["config_definitions"] = [
                {
                    "id": str(definition.id),
                    "name": definition.name,
                    "key": definition.key,
                    "value_type": definition.value_type,
                    "description": definition.description,
                    "is_required": definition.is_required,
                    "is_secret": definition.is_secret,
                }
                for definition in provider.config_definitions.all()
            ]
        return data

    @classmethod
    def _provider_account_data(cls, account: ProviderAccount) -> dict:
        definitions = {
            definition.key: definition for definition in account.provider.config_definitions.all()
        }
        configs = {}
        for entry in account.config_entries.all():
            definition = definitions.get(entry.key)
            is_secret = definition.is_secret if definition else False
            configs[entry.key] = {
                "value": None if is_secret else entry.typed_value,
                "value_type": entry.value_type,
                "is_secret": is_secret,
                "is_configured": bool(entry.value),
            }
        return {
            "id": str(account.id),
            "name": account.name,
            "organization_remote_code": account.organization_remote_code,
            "environment": account.environment,
            "is_active": account.is_active,
            "provider": cls._provider_data(account.provider),
            "configs": configs,
        }

    @classmethod
    def _override_data(cls, config, event_type: str) -> dict:
        event = (
            config.inbound_payment_event
            if event_type == cls.EVENT_TYPE_PAYMENT
            else config.outbound_transfer_event
        )
        return {
            "id": str(config.id),
            "event_type": event_type,
            "event": {"id": str(event.id), "slug": event.slug, "name": event.name},
            "organization_remote_code": config.organization_remote_code,
            "is_active": config.is_active,
            "provider": cls._provider_data(config.provider),
            "provider_config_id": str(config.provider_account_id),
        }

    @classmethod
    def _resolve_event(
        cls,
        *,
        system: System,
        event_slug: str,
        event_type: str | None,
    ):
        event_type = cls._event_type(event_type, required=False)
        matches = []
        if event_type in (None, cls.EVENT_TYPE_PAYMENT):
            event = InboundPaymentEvent.objects.filter(
                system=system,
                slug=event_slug,
                is_active=True,
            ).first()
            if event:
                matches.append((event, cls.EVENT_TYPE_PAYMENT))
        if event_type in (None, cls.EVENT_TYPE_TRANSFER):
            event = (
                OutboundTransferEvent.objects.select_related("payment_method_type")
                .filter(
                    system=system,
                    slug=event_slug,
                    is_active=True,
                )
                .first()
            )
            if event:
                matches.append((event, cls.EVENT_TYPE_TRANSFER))
        if not matches:
            raise ObjectDoesNotExist(
                f"Active payment or transfer event '{event_slug}' not found for system "
                f"'{system.name}'."
            )
        if len(matches) > 1:
            raise ValidationError(
                "event_type is required because the event slug exists for both payments and "
                "transfers."
            )
        return matches[0]

    @staticmethod
    def _event_type(value: str | None, *, required: bool) -> str | None:
        if value is None or not str(value).strip():
            if required:
                raise ValidationError("event_type is required.")
            return None
        value = str(value).strip().lower()
        if value not in ProviderConfigurationService.EVENT_TYPES:
            raise ValidationError("event_type must be 'payment' or 'transfer'.")
        return value

    @staticmethod
    def _organization_remote_code(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("organization_remote_code is required.")
        value = value.strip()
        if len(value) > 40:
            raise ValidationError("organization_remote_code cannot exceed 40 characters.")
        validate_slug(value)
        return value

    @classmethod
    def _validate_configs(
        cls,
        provider: Provider,
        supplied_configs: dict,
    ) -> list[tuple[ProviderConfigDefinition, str]]:
        definitions = {item.key: item for item in provider.config_definitions.all()}
        required = {key for key, item in definitions.items() if item.is_required}
        supplied = set(supplied_configs)
        missing = sorted(required - supplied)
        unknown = sorted(supplied - set(definitions))
        if missing or unknown:
            raise ValidationError(
                {
                    "configs": (
                        "Configs must match the provider definitions. "
                        f"Missing: {missing or 'none'}; unknown: {unknown or 'none'}."
                    )
                }
            )
        return [
            (definitions[key], cls._serialize_config_value(definitions[key], value))
            for key, value in supplied_configs.items()
        ]

    @staticmethod
    def _serialize_config_value(definition: ProviderConfigDefinition, value) -> str:
        value_type = definition.value_type
        if value_type == ProviderConfigDefinition.ValueType.TEXT:
            if not isinstance(value, str):
                raise ValidationError(f"Config '{definition.key}' must be a string.")
            return value
        if value_type == ProviderConfigDefinition.ValueType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"Config '{definition.key}' must be an integer.")
            return str(value)
        if value_type == ProviderConfigDefinition.ValueType.DECIMAL:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"Config '{definition.key}' must be a number.")
            if not math.isfinite(value):
                raise ValidationError(f"Config '{definition.key}' must be finite.")
            return str(value)
        if value_type == ProviderConfigDefinition.ValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise ValidationError(f"Config '{definition.key}' must be a boolean.")
            return "true" if value else "false"
        try:
            return json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Config '{definition.key}' must be valid JSON.") from exc
