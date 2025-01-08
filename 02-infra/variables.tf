variable "telegram_token" {
  description = "Telegram Bot API Token"
  type        = string
  sensitive   = true
}

variable "project_id" {
  description = "Google Cloud Project ID"
  type        = string
}

variable "service_url" {
  description = "Public URL where the service will be accessible"
  type        = string
}

variable "whoop_client_id" {
  description = "WHOOP OAuth Client ID"
  type        = string
  sensitive   = true
}

variable "whoop_client_secret" {
  description = "WHOOP OAuth Client Secret"
  type        = string
  sensitive   = true
}

variable "container_image" {
  description = "The container image to deploy"
  type        = string
}

variable "service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "gym-bro"
}

variable "region" {
  description = "GCP region to deploy to"
  type        = string
  default     = "europe-west3"
}

variable "cpu_limit" {
  description = "CPU limit for the container"
  type        = string
  default     = "1000m"
}

variable "memory_limit" {
  description = "Memory limit for the container"
  type        = string
  default     = "512Mi"
}