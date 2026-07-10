output "project_id" {
  description = "Railway project id (use it for `railway link` and CLI ops)."
  value       = railway_project.sysdesign.id
}

output "environment_id" {
  description = "production environment id."
  value       = railway_project.sysdesign.default_environment.id
}

output "api_url" {
  description = "Public URL of the FastAPI service. This is the tool surface the Module 5 managed agent curls."
  value       = "https://${railway_service_domain.api.domain}"
}
