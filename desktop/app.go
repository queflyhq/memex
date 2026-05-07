// Wails Go backend for memex-desktop. Talks to the local memex daemon
// over HTTP — never opens DuckDB directly (single-writer rule).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// App is the Wails-bound struct exposing methods to the Svelte frontend.
type App struct {
	ctx        context.Context
	daemonURL  string
	authToken  string
	httpClient *http.Client
}

// NewApp creates a new App application struct.
func NewApp() *App {
	return &App{
		httpClient: &http.Client{Timeout: 15 * time.Second},
	}
}

// startup runs once when the Wails window opens. Resolves the daemon
// URL + auth token from the same files the Python CLI writes to, and
// auto-spawns a daemon if one isn't responding.
func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
	a.daemonURL = "http://127.0.0.1:7777"
	a.authToken = ""
	a.resolveDaemonConfig()
	if !a.daemonAlive() {
		_ = a.spawnDaemon()
		// Give it a moment to come up.
		for i := 0; i < 20; i++ {
			time.Sleep(250 * time.Millisecond)
			if a.daemonAlive() {
				break
			}
		}
	}
}

func (a *App) resolveDaemonConfig() {
	dir := daemonDataDir()
	if b, err := os.ReadFile(filepath.Join(dir, "daemon.url")); err == nil {
		a.daemonURL = strings.TrimSpace(string(b))
	}
	if b, err := os.ReadFile(filepath.Join(dir, "daemon.token")); err == nil {
		a.authToken = strings.TrimSpace(string(b))
	}
}

func daemonDataDir() string {
	switch runtime.GOOS {
	case "windows":
		if v := os.Getenv("LOCALAPPDATA"); v != "" {
			return filepath.Join(v, "Quefly", "memex")
		}
	case "darwin":
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, "Library", "Application Support", "Quefly", "memex")
		}
	default:
		if v := os.Getenv("XDG_DATA_HOME"); v != "" {
			return filepath.Join(v, "memex")
		}
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, ".local", "share", "memex")
		}
	}
	return ""
}

func (a *App) daemonAlive() bool {
	req, err := http.NewRequestWithContext(a.ctx, http.MethodGet, a.daemonURL+"/health", nil)
	if err != nil {
		return false
	}
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func (a *App) spawnDaemon() error {
	// Use the user's existing memex.bat / memex command if present.
	candidates := []string{"memex"}
	if runtime.GOOS == "windows" {
		candidates = append([]string{"memex.bat"}, candidates...)
	}
	for _, name := range candidates {
		path, err := exec.LookPath(name)
		if err != nil {
			continue
		}
		cmd := exec.Command(path, "daemon")
		_ = cmd.Start()
		return nil
	}
	return fmt.Errorf("memex CLI not found in PATH — install memex first")
}

func (a *App) applyAuth(req *http.Request) {
	if a.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+a.authToken)
	}
}

// ---- Wails-bound methods (called from Svelte via wailsjs) -------------------

// GetDaemonStatus reports whether the local daemon is reachable.
func (a *App) GetDaemonStatus() map[string]any {
	if a.daemonAlive() {
		return map[string]any{"alive": true, "url": a.daemonURL}
	}
	return map[string]any{"alive": false, "url": a.daemonURL}
}

// GetStats fetches the rich Dashboard / Impact payload.
// Falls back to /progress + /tasks when the daemon doesn't yet expose
// /stats (older daemon binary still serving).
func (a *App) GetStats(windowHours int) (map[string]any, error) {
	q := ""
	if windowHours > 0 {
		q = fmt.Sprintf("?window_hours=%d", windowHours)
	}
	body, status, err := a.daemonGet("/stats" + q)
	if err == nil && status == http.StatusOK {
		var out map[string]any
		if err := json.Unmarshal(body, &out); err != nil {
			return nil, err
		}
		return out, nil
	}
	// Fallback for older daemons.
	return a.statsFallback()
}

func (a *App) statsFallback() (map[string]any, error) {
	progBody, _, err := a.daemonGet("/progress")
	if err != nil {
		return nil, err
	}
	var prog map[string]any
	_ = json.Unmarshal(progBody, &prog)
	out := map[string]any{
		"concepts_total": prog["concepts_total"],
		"events_total":   prog["events_total"],
		"impact": map[string]any{
			"validations_count": prog["validations_count"],
			"validated_skills":  prog["validated_skills"],
		},
		"degraded":        true,
		"degraded_reason": "/stats endpoint not yet served by daemon — restart memex daemon to pick it up",
	}
	return out, nil
}

// GetConcepts pages through concepts (optional kind filter).
func (a *App) GetConcepts(kind string, limit int, offset int) (map[string]any, error) {
	if limit <= 0 {
		limit = 100
	}
	q := fmt.Sprintf("?limit=%d&offset=%d", limit, offset)
	if kind != "" {
		q += "&kind=" + kind
	}
	body, _, err := a.daemonGet("/concepts" + q)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetEdgesFor returns every edge touching a concept.
func (a *App) GetEdgesFor(conceptID string) (map[string]any, error) {
	body, _, err := a.daemonGet("/edges/" + conceptID)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetEvents returns episodic events (optional kind filter).
func (a *App) GetEvents(kind string, limit int) (map[string]any, error) {
	if limit <= 0 {
		limit = 100
	}
	q := fmt.Sprintf("?limit=%d", limit)
	if kind != "" {
		q += "&kind=" + kind
	}
	body, _, err := a.daemonGet("/events" + q)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// Recall calls the daemon's /recall endpoint with a free-form query.
func (a *App) Recall(query string, budget int, expand int) (map[string]any, error) {
	if budget <= 0 {
		budget = 2000
	}
	q := fmt.Sprintf("?q=%s&budget=%d&expand=%d", encode(query), budget, expand)
	body, _, err := a.daemonGet("/recall" + q)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func encode(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, " ", "%20"), "&", "%26")
}

// ---- HTTP helper ------------------------------------------------------------

func (a *App) daemonGet(path string) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(a.ctx, http.MethodGet, a.daemonURL+path, nil)
	if err != nil {
		return nil, 0, err
	}
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	return body, resp.StatusCode, nil
}
