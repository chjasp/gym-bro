# Create service account
resource "google_service_account" "app_sa" {
  project      = var.project_id
  account_id   = "${var.service_name}-sa"
  display_name = "Service Account for ${var.service_name}"
}

# Grant Firestore Admin role to the service account
resource "google_project_iam_member" "firestore_admin" {
  project = var.project_id
  role    = "roles/datastore.owner"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Grant AI Platform Predict role to the service account
resource "google_project_iam_member" "ai_platform_predict" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# Cloud Run service
resource "google_cloud_run_service" "app" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  template {
    spec {
      service_account_name = google_service_account.app_sa.email
      containers {
        image = var.container_image
        
        resources {
          limits = {
            cpu    = var.cpu_limit
            memory = var.memory_limit
          }
        }

        # Add environment variables
        env {
          name  = "TELEGRAM_TOKEN"
          value = var.telegram_token
        }
        
        env {
          name  = "GCP_PROJECT_ID"
          value = var.project_id
        }

        env {
          name  = "URL"
          value = var.service_url
        }

        env {
          name  = "WHOOP_CLIENT_ID"
          value = var.whoop_client_id
        }

        env {
          name  = "WHOOP_CLIENT_SECRET"
          value = var.whoop_client_secret
        }

        env {
          name  = "BOT_MODE"
          value = "webhook"  # Force webhook mode in Cloud Run
        }
      }
    }
  }

  metadata {
    annotations = {"run.googleapis.com/ingress" = "all"}
  }
}

# IAM policy binding to allow public access
resource "google_cloud_run_service_iam_member" "public_access" {
  project  = var.project_id
  service  = google_cloud_run_service.app.name
  location = google_cloud_run_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_scheduler_job" "morning_motivation_job" {
  project          = var.project_id
  region           = var.region
  name             = "morning-motivation-trigger"
  description      = "Triggers morning motivation messages at 6:31 AM Berlin time"
  schedule         = "31 6 * * *"
  time_zone        = "Europe/Berlin"
  attempt_deadline = "320s"

  http_target {
    http_method = "GET"
    uri         = "${google_cloud_run_service.app.status[0].url}/morning_motivation"

    oidc_token {
      service_account_email = google_service_account.scheduler_service_account.email
      audience             = google_cloud_run_service.app.status[0].url
    }
  }
}

# Service account for the scheduler
resource "google_service_account" "scheduler_service_account" {
  project      = var.project_id
  account_id   = "scheduler-sa"
  display_name = "Cloud Scheduler Service Account"
}

# IAM binding to allow the scheduler service account to invoke Cloud Run
resource "google_cloud_run_service_iam_member" "scheduler_invoker" {
  project      = var.project_id
  location = google_cloud_run_service.app.location
  service  = google_cloud_run_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_service_account.email}"
}