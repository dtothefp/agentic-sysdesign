# All secrets come in through tfvars (gitignored) or env vars, never hardcoded.
# See terraform.tfvars.example for the shape.

variable "railway_token" {
  description = "Railway ACCOUNT/workspace token (railway.com/account/tokens). Leave empty to use the RAILWAY_TOKEN env var instead."
  type        = string
  default     = ""
  sensitive   = true
}

variable "railway_workspace_id" {
  description = "Railway workspace/team id. Required only when the token can see more than one workspace; leave empty for a single-workspace token."
  type        = string
  default     = ""
}

variable "project_name" {
  description = "Name of the Railway project this config owns."
  type        = string
  default     = "sysdesign"
}

variable "github_repo" {
  description = "owner/name of the GitHub repo Railway builds the api + worker from. The Railway GitHub app must be installed on it."
  type        = string
  default     = "dfp-side-hustle/sysdesign"
}

variable "github_branch" {
  description = "Branch Railway deploys from."
  type        = string
  default     = "main"
}

variable "api_subdomain" {
  description = "Subdomain for the API's public *.up.railway.app URL. Must be globally unique on Railway; change it if apply reports a collision."
  type        = string
  default     = "sysdesign-api"
}

# ---- app runtime secrets, injected as service variables ----

variable "database_url" {
  description = "Supabase session-pooler connection string (postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require). From backend/.env DATABASE_URL_SUPABASE."
  type        = string
  sensitive   = true
}

variable "apify_api_key" {
  description = "Apify REST token for the live scrape path (worker only)."
  type        = string
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Anthropic API key for the Module 3 rating stage (worker). Leave empty until Module 3 lands."
  type        = string
  default     = ""
  sensitive   = true
}
