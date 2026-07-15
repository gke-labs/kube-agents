# План тестирования E2E автотеста через GitHub Actions (на origin репозитории)

Документ содержит пошаговый план настройки **Workload Identity Federation (WIF)** и проверки работы E2E автотеста (`tests/e2e/gchat_agent_test.py`) через **GitHub Actions** в репозитории `gke-agentic/kube-agents`.

---

## 📌 Цели
1. Настроить безопасную безключевую аутентификацию (Keyless Auth via WIF) между GitHub Actions и GCP.
2. Проверить удаленный запуск E2E теста через вкладку **Actions** в GitHub (`workflow_dispatch`).
3. Убедиться, что автотест успешный в CI/CD среде и публикует сообщения в Google Chat.

---

## 📋 Пошаговый План Реализации

### Шаг 1. Создание Service Account в GCP & Назначение Ролей IAM

Выполните команды в терминале GCP (под вашим проектом `$PROJECT_ID`):

```bash
# 1. Задайте переменные
export PROJECT_ID=$(gcloud config get-value project)
export SA_NAME="github-actions-e2e"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export CHAT_TOPIC_NAME="platform-agent-chat-events"

# 2. Создайте сервисный аккаунт
gcloud iam service-accounts create $SA_NAME \
    --display-name="GitHub Actions E2E Runner Service Account"

# 3. Выдайте права на публикацию в топик Pub/Sub (Least Privilege)
gcloud pubsub topics add-iam-policy-binding "$CHAT_TOPIC_NAME" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/pubsub.publisher"

# 4. Выдайте права на отправку сообщений в Google Chat API
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/chat.admin"
```

---

### Шаг 2. Настройка Workload Identity Federation (WIF) в GCP

Настройка позволяет GitHub Actions подключаться к GCP без долгоживущих JSON-ключей:

```bash
# 1. Включите необходимые API
gcloud services enable iamcredentials.googleapis.com cloudresourcemanager.googleapis.com

# 2. Создайте Workload Identity Pool
gcloud iam workload-identity-pools create "github-actions-pool" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --display-name="GitHub Actions Pool"

# 3. Получите ID созданного пула
export WORKLOAD_IDENTITY_POOL_ID=$(gcloud iam workload-identity-pools describe "github-actions-pool" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --format="value(name)")

# 4. Создайте Provider для GitHub Actions
gcloud iam workload-identity-pools providers create-oidc "github-provider" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --workload-identity-pool="github-actions-pool" \
    --display-name="GitHub Actions OIDC Provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --issuer-uri="https://token.actions.githubusercontent.com"

# 5. Привяжите ваш GitHub репозиторий (gke-agentic/kube-agents) к Service Account
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/${WORKLOAD_IDENTITY_POOL_ID}/attribute.repository/gke-agentic/kube-agents"
```

---

### Шаг 3. Проверка параметров в `.github/workflows/e2e-gchat-test.yml`

Убедитесь, что workflow содержит верные значения `workload_identity_provider` и `service_account`:

```yaml
      - name: Authenticate to Google Cloud via WIF
        uses: google-github-actions/auth@v3
        with:
          token_format: "access_token"
          workload_identity_provider: "projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-actions-pool/providers/github-provider"
          service_account: "github-actions-e2e@${{ inputs.gcp_project_id }}.iam.gserviceaccount.com"
```

---

### Шаг 4. Настройка Переменных в GitHub (Settings -> Secrets and variables)

В вашем репозитории `https://github.com/gke-agentic/kube-agents`:
1. Перейдите в **Settings** -> **Secrets and variables** -> **Actions** -> **Variables**.
2. Добавьте **Repository Variable**:
   - `GCP_PROJECT_ID`: `eleontev-kube-agents`
   - `CHAT_SPACE_ID`: `spaces/AAQAfrKMyng`

---

### Шаг 5. Запуск и Проверка в GitHub Actions UI

1. Перейдите во вкладку **Actions** в репозитории `gke-agentic/kube-agents`.
2. Выберите **Google Chat Agent E2E Test** в левом меню.
3. Нажмите кнопку **Run workflow** -> выберите ветку `feature/e2e-gchat-test`.
4. Заполните параметры (`gcp_project_id`, `chat_space_id`).
5. Нажмите **Run workflow**.

#### Ожидаемый результат:
- Шаг **Authenticate to Google Cloud via WIF** проходит успешно за ~2 сек.
- Шаг **Execute E2E Test** запускает `pytest`, публикует prompt в треде `spaces/AAQAfrKMyng`, получает ответ от Гермеса (`5`) и завершается со статусом `PASSED`.

---

## 🛠️ Критерии Успеха
- [ ] WIF настроен в GCP без использования JSON-ключей.
- [ ] Workflow в GitHub Actions успешно проходит от начала до конца.
- [ ] В треде Google Chat появляется ответ агента.
