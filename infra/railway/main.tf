# Module 4 deploy topology on Railway: one project, three services in the production
# environment. api (public FastAPI) + worker (Celery, beat embedded) both build from the
# GitHub repo's backend/ dir; redis is the broker + SSE pub/sub bus, private-network only.
# Postgres is NOT here, it lives on Supabase (the DATABASE_URL variable points at the pooler).

resource "railway_project" "sysdesign" {
  name         = var.project_name
  private      = true
  workspace_id = var.railway_workspace_id != "" ? var.railway_workspace_id : null

  # New projects already ship a "production" environment; naming it here just pins the
  # reference so we can read its computed id for variables and the domain below.
  default_environment = {
    name = "production"
  }
}

# ---- Redis (broker + result backend + progress pub/sub) ----
# Image services can't take a start command or a config file through this provider (no
# start-command field, and config_path conflicts with source_image), so Redis auth has to
# come from the image's own env contract. The bitnami image reads REDIS_PASSWORD and enables
# requirepass, which also clears the default protected-mode block that would otherwise reject
# the worker's cross-service (non-loopback) connection over Railway's private network.
resource "random_password" "redis" {
  length  = 32
  special = false
}

resource "railway_service" "redis" {
  name         = "redis"
  project_id   = railway_project.sysdesign.id
  source_image = "bitnami/redis:7.4"
}

resource "railway_variable" "redis_password" {
  environment_id = railway_project.sysdesign.default_environment.id
  service_id     = railway_service.redis.id
  name           = "REDIS_PASSWORD"
  value          = random_password.redis.result
}

# ---- API (public FastAPI surface) ----
resource "railway_service" "api" {
  name               = "api"
  project_id         = railway_project.sysdesign.id
  source_repo        = var.github_repo
  source_repo_branch = var.github_branch
  root_directory     = "/backend"
  config_path        = "railway.api.json"
}

resource "railway_service_domain" "api" {
  environment_id = railway_project.sysdesign.default_environment.id
  service_id     = railway_service.api.id
  subdomain      = var.api_subdomain
}

# ---- Worker (Celery worker with embedded beat) ----
resource "railway_service" "worker" {
  name               = "worker"
  project_id         = railway_project.sysdesign.id
  source_repo        = var.github_repo
  source_repo_branch = var.github_branch
  root_directory     = "/backend"
  config_path        = "railway.worker.json"
}

# ---- Service variables ----
# Redis is addressed by its Railway private domain, resolved at deploy via a reference
# variable ($${{redis.RAILWAY_PRIVATE_DOMAIN}}). The $${ escape emits a literal ${ so the
# stored value is the Railway token, not a Terraform interpolation. The password IS a real
# Terraform interpolation from random_password above.
locals {
  redis_base = "redis://:${random_password.redis.result}@$${{redis.RAILWAY_PRIVATE_DOMAIN}}:6379"

  # celery_app.py reads CELERY_BROKER_URL (db 0) + CELERY_RESULT_BACKEND (db 1);
  # tasks.py and api/main.py read REDIS_URL for the progress pub/sub channel.
  common_vars = {
    DATABASE_URL          = var.database_url
    REDIS_URL             = local.redis_base
    CELERY_BROKER_URL     = "${local.redis_base}/0"
    CELERY_RESULT_BACKEND = "${local.redis_base}/1"
  }

  api_vars = local.common_vars

  worker_vars = merge(
    local.common_vars,
    { APIFY_API_KEY = var.apify_api_key },
    var.anthropic_api_key != "" ? { ANTHROPIC_API_KEY = var.anthropic_api_key } : {},
  )
}

resource "railway_variable" "api" {
  for_each       = local.api_vars
  environment_id = railway_project.sysdesign.default_environment.id
  service_id     = railway_service.api.id
  name           = each.key
  value          = each.value
}

resource "railway_variable" "worker" {
  for_each       = local.worker_vars
  environment_id = railway_project.sysdesign.default_environment.id
  service_id     = railway_service.worker.id
  name           = each.key
  value          = each.value
}
