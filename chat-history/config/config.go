package config

import (
	"encoding/json"
	"os"
)

type LLMConfig struct {
	ModelName string `json:"model_name"`
}

type ChatDbConfig struct {
	Port                    string   `json:"apiPort"`
	DbPath                  string   `json:"dbPath"`
	DbLogPath               string   `json:"dbLogPath"`
	LogPath                 string   `json:"logPath"`
	ConversationAccessRoles []string `json:"conversationAccessRoles"`
}

type TgDbConfig struct {
	Hostname string `json:"hostname"`
	Username string `json:"username"`
	Password string `json:"password"`
	GsPort   string `json:"gsPort"`
	// GetToken string `json:"getToken"`
	// DefaultTimeout       string `json:"default_timeout"`
	// DefaultMemThreshold string `json:"default_mem_threshold"`
	// DefaultThreadLimit  string `json:"default_thread_limit"`
}

type Config struct {
	TgDbConfig TgDbConfig `json:"db_config"`
	ChatDbConfig ChatDbConfig `json:"chat_config"`
	// LLMConfig LLMConfig `json:"llm_config"`
}

func LoadConfig(paths map[string]string) (Config, error) {
	var config Config

        if config_path, ok := paths["tgconfig"]; ok {
	        b, err := os.ReadFile(config_path)
	        if err != nil {
			return Config{}, err
	        }
	        if err := json.Unmarshal(b, &config); err != nil {
		        return Config{}, err
	        }
        }
	return config, nil
}
