# ==============================================================================
# Quality drift alerting — Cloud Monitoring alert policy on Online Monitor scores
# ==============================================================================
#
# Online Monitors (Gemini Enterprise Agent Platform's continuous agentic-quality
# eval over live production traces) have no creation API — no Terraform
# resource, no gcloud command, no SDK method. They're console-only: Agent
# Platform > Agents > Evaluation > Online monitors > New monitor. This module
# can't create the monitor itself, but the metric it exports
# (aiplatform.googleapis.com/online_evaluator/scores) is a normal Cloud
# Monitoring time series, and the ALERT POLICY watching it is a normal,
# fully-Terraform-managed resource.
#
# This is deliberately separate from — and complementary to — the
# canary-quality-check pipeline (cicd.tf):
#   - canary-quality-check answers "is THIS release safe to promote", using
#     real traffic pulled on demand and compared canary-vs-champion.
#   - This alert answers "has the long-lived, already-promoted champion
#     silently drifted" between releases — a slow, ambient failure mode that
#     canary-quality-check never looks at (it holds/no-ops when no canary is
#     live).
#
# Enabling this (enable_quality_alerts = true) only wires the alert policy.
# It does nothing until an Online Monitor exists and is pointed at the
# current champion Agent Engine resource — a one-time manual console step,
# repeated after any release that promotes a new resource to champion. See
# docs/EVALUATION.md's "Ambient Drift Monitoring" section for that runbook.

resource "google_monitoring_alert_policy" "agent_quality_drift" {
  count        = var.enable_quality_alerts && local.is_prod ? 1 : 0
  project      = var.project_id
  display_name = "Agent quality drift — Online Monitor task success"
  combiner     = "OR"

  conditions {
    display_name = "Online Monitor task_success below threshold"

    condition_threshold {
      filter = "metric.type=\"aiplatform.googleapis.com/online_evaluator/scores\" AND metric.labels.evaluation_metric_name=\"task_success\""

      comparison      = "COMPARISON_LT"
      threshold_value = var.quality_alert_task_success_threshold
      duration        = "1800s" # sustained drop, not a single noisy sample

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.quality_alert_notification_channels

  documentation {
    content   = "The Online Monitor's task_success score for the prod Agent Engine has averaged below ${var.quality_alert_task_success_threshold} for 30+ minutes. This is ambient quality drift on the live champion, not a canary release event — see docs/EVALUATION.md's Ambient Drift Monitoring section for context and the Online Monitor console link."
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}
