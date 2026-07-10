# The Railway provider needs an ACCOUNT / workspace token, not a project token.
# Create one at https://railway.com/account/tokens (Account settings, not project
# settings). The project-scoped token that drives the Railway CLI cannot manage
# resources through the API the provider uses.
#
# Supply it either by exporting RAILWAY_TOKEN in the shell, or by setting the
# railway_token variable. Leaving the token argument unset here lets the provider
# fall back to the RAILWAY_TOKEN env var, which keeps the secret out of tfvars.
provider "railway" {
  token = var.railway_token != "" ? var.railway_token : null
}
