# Session Isolation, Profiles, and Vector Memory Scoping

This document details how session isolation, profile environments, and user-level memory partitioning work in Hermes, specifically when integrated with Google Chat. It also provides a deployment guide for setting up a self-hosted **Mem0** memory provider with **Qdrant** in Kubernetes.

---

## 1. Session Isolation in Google Chat

Hermes isolates conversations using a dynamically generated `session_key` constructed by the gateway adapter based on metadata from the inbound message.

### Session Isolation Boundaries:
*   **Direct Messages (DMs)**: Isolated per user. The session key uses the sender's unique user ID, ensuring that different users have completely separate DM transcripts.
*   **Space Threads**: Isolated per thread. Messages within the same Google Chat thread share a single session key and conversation history.
*   **Space Non-Threads**: Isolated per user. Messages without thread context default to isolating by the user ID.

### Sharing vs. Isolation:
*   **Isolated**: Conversation transcripts, token counts, and session databases (`state.db` entries) are strictly isolated.
*   **Shared**: The agent's persistent memory (learned facts and preferences) is loaded from the active profile's `memories/` directory and is shared across all sessions running on that profile.

---

## 2. Profiles in Hermes

A **profile** is a fully independent instance of the agent's workspace. It isolates configuration, credentials, logs, and memories at the filesystem level.

### Directory Structure:
*   **Default Profile**: Located at `~/.hermes/` (or your custom `HERMES_HOME` path like `/opt/data/`).
*   **Named Profiles**: Located at `~/.hermes/profiles/<profile_name>/`.

### Memory Isolation:
*   Memory files (`MEMORY.md` and `USER.md`) live under the active profile's `memories/` directory.
*   Profile A cannot read or write to Profile B's memories.
*   However, **all sessions within the same profile share the same memories**.

---

## 3. Separating Memories for Group Chat Users

When multiple users (e.g., User A and User B) talk to Hermes in a shared Google Chat group, they run under the same gateway adapter profile (`default`).

### Why Built-in Memory Fails to Separate Users:
The built-in memory store reads/writes directly to the profile's flat `/opt/data/memories/USER.md` file. It does not look at the sender's user ID. Thus, facts learned from User A will bleed into the context for User B.

### The Solution: User-Aware Memory Plugins
To separate memories, you must use an external memory provider plugin (like **Mem0**) that supports vector-based partitioning:
1. When a message is received, the Google Chat adapter extracts the sender's Google User ID (e.g., `users/112233...`) and passes it as `user_id` when initializing the agent.
2. The memory plugin receives this `user_id` and namespaces all database queries and updates accordingly.
3. User A and User B's memories are strictly separated within the same bot connection.

---

## 4. Architecture Overview: Mem0 with Qdrant and LiteLLM

To achieve scalable, isolated session memory, we integrated the **Mem0** memory framework running in self-hosted (OSS) mode, backed by a local **Qdrant** vector database and routed through a local **LiteLLM** proxy.

The flow for memory storage and retrieval operates as follows:
1. **Agent Turn**: When an agent processes a message, it interacts with the `Mem0MemoryProvider` plugin.
2. **Embeddings**: Mem0 generates text embeddings using an OpenAI-compatible client library pointing to the local LiteLLM proxy.
3. **Storage**: Vectors and payload metadata (such as `user_id` and `agent_id`) are stored in the Qdrant service.
4. **Retrieval**: During subsequent turns, the agent queries the vector database for relevant past context filtered by the user identifiers.

---

## 5. Mem0 Configuration (`mem0.json`)

The default configuration file is located at [deploy/shared/defaults/mem0.json](deploy/shared/defaults/mem0.json).

```json
{
  "mode": "oss",
  "oss": {
    "llm": {
      "provider": "openai",
      "config": {
        "model": "model-default",
        "api_key": "none",
        "openai_base_url": "http://litellm.agent-system.svc.cluster.local/v1"
      }
    },
    "embedder": {
      "provider": "openai",
      "config": {
        "model": "gemini-embedding-2",
        "api_key": "none",
        "openai_base_url": "http://litellm.agent-system.svc.cluster.local/v1",
        "embedding_dims": 3072
      }
    },
    "vector_store": {
      "provider": "qdrant",
      "config": {
        "url": "http://qdrant-service.agent-system.svc.cluster.local:6333",
        "collection_name": "mem0"
      }
    }
  }
}
```

### Key Technical Details:
*   **Self-Hosted mode (`mode: "oss"`)**: Configures Mem0 to bypass the cloud platform API (which requires a developer key) and run entirely on local cluster endpoints.
*   **Gemini Embedding Model (`gemini-embedding-2`)**: Gemini developer API keys (AI Studio) do not support the Vertex AI namespace `text-embedding-004`. Instead, they use `gemini-embedding-2`.
*   **Vector Dimensions (`embedding_dims: 3072`)**: The `gemini-embedding-2` model produces vectors of **3072 dimensions**. Qdrant must be explicitly told to initialize the `mem0` collection with `3072` dimensions. Leaving this unconfigured triggers a default fallback of `1536` dimensions, causing a `400 Bad Request` schema mismatch error during vector writes.

---

## 6. LiteLLM Proxy Configuration Requirements

The LiteLLM deployment manages request routing to the Gemini API. To support vector embeddings, the following modifications must be applied to the LiteLLM ConfigMap.

These have been integrated into the provisioning templates in the repository:
*   [k8s-operator/config/integrations/litellm/configmap.yaml](k8s-operator/config/integrations/litellm/configmap.yaml) (Operator integration template)
*   [integrations/gchat/provision_litellm/configmap.yaml.tmpl](integrations/gchat/provision_litellm/configmap.yaml.tmpl) (Google Chat provisioner template)

### Configuration Schema:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-config
data:
  config.yaml: |
    model_list:
      - model_name: model-default
        litellm_params:
          model: ${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}
          api_key: os.environ/GEMINI_API_KEY
      - model_name: gemini-embedding-2
        litellm_params:
          model: gemini/gemini-embedding-2
          api_key: os.environ/GEMINI_API_KEY
    litellm_settings:
      drop_params: true
      callbacks: ["otel", "prometheus"]
```

### Why these settings are needed:

1.  **`drop_params: true` under `litellm_settings` (CRITICAL)**:
    *   **The Issue**: The standard OpenAI python library used by Mem0 appends `encoding_format: "float"` by default to embedding requests. LiteLLM forwards this parameter to the upstream Google Gemini API, which rejects it with a `400 BadRequest` error (`Unsupported parameter: encoding_format`).
    *   **The Solution**: Setting `drop_params: true` instructs LiteLLM to automatically strip any client parameters that are not supported by the destination provider (Gemini) before routing the request.
2.  **`gemini-embedding-2` model mapping**:
    *   Adds a dedicated route for the embedding model, pointing to `gemini/gemini-embedding-2` and passing your `GEMINI_API_KEY`.

---

## 7. Operational Troubleshooting Playbook

If vector memory errors are encountered (e.g. in the `platform-agent` logs or during synchronization tasks):

1.  **Check Vector Database Dimension Schema**:
    Verify that the Qdrant `mem0` collection matches the 3072-dimension layout:
    ```bash
    kubectl exec -i $(kubectl get pods -n agent-system -l app=platform-agent -o jsonpath='{.items[0].metadata.name}') -n agent-system -c platform-agent -- \
      curl -s http://qdrant-service.agent-system.svc.cluster.local:6333/collections/mem0
    ```
    Ensure that `"vectors": {"size": 3072}` is returned.
2.  **Reset/Recreate Collections**:
    If Qdrant was previously initialized with 1536 dimensions, the collection must be deleted so Mem0 can recreate it with 3072 dimensions:
    ```bash
    kubectl exec -i $(kubectl get pods -n agent-system -l app=platform-agent -o jsonpath='{.items[0].metadata.name}') -n agent-system -c platform-agent -- \
      curl -X DELETE http://qdrant-service.agent-system.svc.cluster.local:6333/collections/mem0
    ```
