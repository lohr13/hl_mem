# Provider Plugin API

HL-Mem 1.x exposes a governed, in-process Provider extension API for LLM, Embedding and Reranker implementations. Image
description uses the same mechanism as an explicitly experimental preview. The frozen machine-readable contract is
`provider-plugin-api.json`; CI verifies it with `scripts/check_provider_plugin_api.py`.

## Trust and scope

Provider plugins are normal Python distributions loaded into the HL-Mem process. They are **not sandboxed**. An enabled
plugin can execute Python code and can receive the credential for its selected model endpoint, so install and enable only
trusted distributions.

The API is intentionally narrower than a general plugin framework. A Provider plugin may translate neutral model calls
into `ProviderRequest` objects and parse `ProviderResponse` objects. It cannot register REST routes, CLI commands, jobs,
database migrations, storage backends, or security policies. The host owns HTTP execution, retry, timeout, normalized
errors, audit, metrics and atomic usage accounting. Image plugins receive validated bytes/MIME/hash, never the source URI
or file path.

## Capabilities

| Capability | 1.x status | Host-governed usage unit |
|---|---|---|
| `llm` | stable | requests and tokens |
| `embedding` | stable | requests, tokens and embedded items |
| `reranker` | stable | requests, tokens and reranked documents |
| `image_describer` | experimental | requests, tokens and images |

Experimental Image contracts may change in a minor release with changelog and migration notes. The three stable contracts
follow the 1.x deprecation policy in `compatibility.md`.

## Distribution contract

A distribution exposes one entry point per plugin ID in the fixed group `hl_mem.providers`:

```toml
[project.entry-points."hl_mem.providers"]
"acme.models" = "acme_hl_mem:plugin"
```

The target is a zero-argument function returning `ProviderPlugin`. Plugin code imports only from `hl_mem.plugins`:

```python
from hl_mem.plugins import (
    PROVIDER_API_VERSION,
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderKey,
    ProviderManifest,
    ProviderPlugin,
    ProviderStability,
)

def make_acme_adapter(context):
    return AcmeLLMAdapter(context.plugin_options)

def plugin() -> ProviderPlugin:
    key = ProviderKey(ProviderCapability.LLM, "acme")
    manifest = ProviderManifest(
        id="acme.models",
        version="1.0.0",
        api_version=PROVIDER_API_VERSION,
        requires_hl_mem=">=1,<2",
        capabilities=(ProviderCapabilitySpec("acme", ProviderCapability.LLM, ProviderStability.STABLE),),
        config_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    return ProviderPlugin(manifest, {key: make_acme_adapter})
```

The committed fixture under `tests/fixtures/provider_plugin/` is the complete executable example.

## Enablement and configuration

Installation does not enable code. HL-Mem first filters entry-point metadata against the explicit `plugins.enabled`
allowlist and imports only the selected IDs:

```toml
[plugins]
enabled = ["acme.models"]

[plugins.acme.models]
region = "cn"

[llm]
provider = "acme"
```

`[plugins.<id>]` is an open, plugin-owned non-secret namespace validated against the manifest JSON Schema. Secret-like
option names and remote schema references are rejected. Service credentials continue to use the core environment names
(`LLM_API_KEY`, `EMBEDDING_API_KEY`, `RERANKER_API_KEY`, `IMAGE_API_KEY`).

Startup fails closed for a missing or duplicate entry point, entry-point/manifest ID mismatch, incompatible API/core
version, invalid config schema/options, duplicate capability key, wrong adapter shape, or configured Provider that does
not exist. Built-ins pass through the same Registry and collision rules.

## Diagnostics

`hl-mem doctor` resolves enabled plugins without creating the usage sidecar, reports the trusted-in-process boundary, and
inspects any existing usage ledger read-only. `/healthz` exposes only plugin ID, capability/name, stability, registration
health and aggregate usage; it does not expose plugin options, endpoints, credentials or model responses.

## Reference implementation evidence

The independently built `hl-mem-provider-dashscope` reference distribution implements stable LLM, Embedding, and
Reranker adapters using only `hl_mem.plugins`. Artifact verification installs released `hl-mem==1.0.0` and the plugin
wheel into a clean Python 3.12 environment, proves that disabled metadata is not imported, enables all three capabilities,
and confirms that a malformed external response does not damage the built-in Registry.

A bounded live smoke additionally selected the external Embedding and Reranker capabilities while retaining the built-in
Zhipu Coding Plan LLM. Both external network paths completed through host-owned transport, budget, audit, retry, and
settlement proxies with zero dangling reservations. The external LLM adapter remains covered by request/response fixtures;
no live-service claim is made for that capability. The reference distribution is integration evidence, not an official
plugin marketplace or a promise to publish that package.
