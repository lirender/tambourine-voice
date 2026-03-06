use anyhow::{Context, Result};
use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tauri_plugin_http::reqwest::{Client, Url};
use tokio::sync::RwLock;

use crate::settings::CleanupPromptSections;

/// Default STT timeout in seconds (matches server's `DEFAULT_TRANSCRIPTION_WAIT_TIMEOUT_SECONDS`)
pub const DEFAULT_STT_TIMEOUT_SECONDS: f64 = 0.5;

#[derive(Debug, Clone, Copy)]
enum ConfigSyncEndpoint {
    Prompts,
    SttTimeout,
    LlmFormatting,
}

impl ConfigSyncEndpoint {
    fn path(self) -> &'static str {
        match self {
            Self::Prompts => "api/config/prompts",
            Self::SttTimeout => "api/config/stt-timeout",
            Self::LlmFormatting => "api/config/llm-formatting",
        }
    }
}

fn build_config_endpoint_url(server_url: &str, endpoint: ConfigSyncEndpoint) -> Result<Url> {
    let mut parsed_server_url =
        Url::parse(server_url).with_context(|| format!("Invalid server URL: {server_url}"))?;

    let server_base_path = parsed_server_url.path().trim_end_matches('/');
    let combined_endpoint_path = if server_base_path.is_empty() {
        format!("/{}", endpoint.path())
    } else {
        format!("{server_base_path}/{}", endpoint.path())
    };
    parsed_server_url.set_path(&combined_endpoint_path);
    parsed_server_url.set_query(None);
    parsed_server_url.set_fragment(None);
    Ok(parsed_server_url)
}

/// Tracks server connection state for config syncing
pub struct ConfigSyncState {
    client: Client,
    server_url: Option<String>,
    client_uuid: Option<String>,
}

impl Default for ConfigSyncState {
    fn default() -> Self {
        Self::new()
    }
}

impl ConfigSyncState {
    pub fn new() -> Self {
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .expect("Failed to create HTTP client"),
            server_url: None,
            client_uuid: None,
        }
    }

    /// Set connection info when connected to server
    pub fn set_connected(&mut self, server_url: String, client_uuid: String) {
        log::info!("Config sync connected: {server_url} (uuid: {client_uuid})");
        self.server_url = Some(server_url);
        self.client_uuid = Some(client_uuid);
    }

    /// Clear connection info when disconnected
    pub fn set_disconnected(&mut self) {
        self.server_url = None;
        self.client_uuid = None;
        log::info!("Config sync disconnected");
    }

    /// Check if connected to a server
    pub fn is_connected(&self) -> bool {
        self.server_url.is_some() && self.client_uuid.is_some()
    }

    async fn send_config_sync_put_request<RequestBody>(
        &self,
        server_url: &str,
        client_uuid: &str,
        endpoint: ConfigSyncEndpoint,
        request_body: &RequestBody,
        operation_name: &str,
    ) -> Result<()>
    where
        RequestBody: Serialize + ?Sized,
    {
        let endpoint_url = build_config_endpoint_url(server_url, endpoint).with_context(|| {
            format!("Failed to build {operation_name} endpoint from {server_url}")
        })?;

        self.client
            .put(endpoint_url.clone())
            .header("X-Client-UUID", client_uuid)
            .json(request_body)
            .send()
            .await
            .with_context(|| format!("Failed to send {operation_name} request to {endpoint_url}"))?
            .error_for_status()
            .with_context(|| {
                format!("Server returned an error for {operation_name} request to {endpoint_url}")
            })?;

        Ok(())
    }

    /// Sync prompt sections to server (best-effort, logs errors)
    pub async fn sync_prompt_sections(&self, sections: &CleanupPromptSections) -> Result<()> {
        let (Some(server_url), Some(client_uuid)) = (&self.server_url, &self.client_uuid) else {
            return Ok(()); // Not connected, skip silently
        };

        self.send_config_sync_put_request(
            server_url,
            client_uuid,
            ConfigSyncEndpoint::Prompts,
            sections,
            "prompt sections sync",
        )
        .await?;

        log::debug!("Synced prompt sections to server");
        Ok(())
    }

    /// Sync STT timeout to server
    pub async fn sync_stt_timeout(&self, timeout_seconds: f64) -> Result<()> {
        #[derive(Serialize)]
        struct TimeoutBody {
            timeout_seconds: f64,
        }

        let (Some(server_url), Some(client_uuid)) = (&self.server_url, &self.client_uuid) else {
            return Ok(()); // Not connected, skip silently
        };

        self.send_config_sync_put_request(
            server_url,
            client_uuid,
            ConfigSyncEndpoint::SttTimeout,
            &TimeoutBody { timeout_seconds },
            "STT timeout sync",
        )
        .await?;

        log::debug!("Synced STT timeout ({timeout_seconds}) to server");
        Ok(())
    }

    /// Sync LLM formatting enabled setting to server
    pub async fn sync_llm_formatting_enabled(&self, enabled: bool) -> Result<()> {
        #[derive(Serialize)]
        struct LlmFormattingBody {
            enabled: bool,
        }

        let (Some(server_url), Some(client_uuid)) = (&self.server_url, &self.client_uuid) else {
            return Ok(()); // Not connected, skip silently
        };

        self.send_config_sync_put_request(
            server_url,
            client_uuid,
            ConfigSyncEndpoint::LlmFormatting,
            &LlmFormattingBody { enabled },
            "LLM formatting sync",
        )
        .await?;

        log::debug!("Synced LLM formatting enabled={enabled} to server");
        Ok(())
    }
}

pub type ConfigSync = Arc<RwLock<ConfigSyncState>>;

pub fn new_config_sync() -> ConfigSync {
    Arc::new(RwLock::new(ConfigSyncState::new()))
}

#[cfg(test)]
mod tests {
    use super::{build_config_endpoint_url, ConfigSyncEndpoint};

    #[test]
    fn build_config_endpoint_url_appends_endpoint_to_root_server_url() {
        let built_url = build_config_endpoint_url("https://host", ConfigSyncEndpoint::Prompts)
            .expect("root server URL should be valid");

        assert_eq!(built_url.as_str(), "https://host/api/config/prompts");
    }

    #[test]
    fn build_config_endpoint_url_preserves_single_segment_server_base_path() {
        let built_url =
            build_config_endpoint_url("https://host/tambourine", ConfigSyncEndpoint::Prompts)
                .expect("base-path server URL should be valid");

        assert_eq!(
            built_url.as_str(),
            "https://host/tambourine/api/config/prompts"
        );
    }

    #[test]
    fn build_config_endpoint_url_preserves_multi_segment_server_base_path() {
        let built_url =
            build_config_endpoint_url("https://host/tambourine/proxy", ConfigSyncEndpoint::Prompts)
                .expect("multi-segment base-path server URL should be valid");

        assert_eq!(
            built_url.as_str(),
            "https://host/tambourine/proxy/api/config/prompts"
        );
    }

    #[test]
    fn build_config_endpoint_url_handles_trailing_slash_in_server_url() {
        let built_url =
            build_config_endpoint_url("https://host/tambourine/", ConfigSyncEndpoint::Prompts)
                .expect("trailing-slash server URL should be valid");

        assert_eq!(
            built_url.as_str(),
            "https://host/tambourine/api/config/prompts"
        );
    }

    #[test]
    fn build_config_endpoint_url_clears_query_and_fragment_from_server_url() {
        let built_url = build_config_endpoint_url(
            "https://host/tambourine/?foo=bar#section",
            ConfigSyncEndpoint::Prompts,
        )
        .expect("server URL with query and fragment should be valid");

        assert_eq!(
            built_url.as_str(),
            "https://host/tambourine/api/config/prompts"
        );
        assert_eq!(built_url.query(), None);
        assert_eq!(built_url.fragment(), None);
    }
}
