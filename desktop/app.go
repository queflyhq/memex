// Wails Go backend for memex-desktop. Talks to the local memex daemon
// over HTTP — never opens DuckDB directly (single-writer rule).
package main

import (
	"bytes"
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

// bytesReader is a tiny helper to wrap a []byte as an io.Reader for HTTP bodies.
func bytesReader(b []byte) io.Reader { return bytes.NewReader(b) }

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

// GetSystemInfo polls the daemon's /system endpoint — RAM, CPU, uptime,
// DB size breakdown. For the desktop's bottom status bar.
func (a *App) GetSystemInfo() (map[string]any, error) {
	body, _, err := a.daemonGet("/system")
	if err != nil {
		return map[string]any{"error": err.Error()}, nil
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return map[string]any{"error": err.Error()}, nil
	}
	return out, nil
}

// GetDaemonLogs returns the most recent N lines from the daemon's
// in-memory log ring buffer.
func (a *App) GetDaemonLogs(limit int) (map[string]any, error) {
	if limit <= 0 {
		limit = 100
	}
	body, _, err := a.daemonGet(fmt.Sprintf("/logs?limit=%d", limit))
	if err != nil {
		return map[string]any{"error": err.Error()}, nil
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return map[string]any{"error": err.Error()}, nil
	}
	return out, nil
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

// EdgesBulk returns all edges for a comma-separated list of concept ids
// in one call (vs N round-trips). Used by the Graph view.
func (a *App) EdgesBulk(ids []string, limit int) (map[string]any, error) {
	if limit <= 0 {
		limit = 5000
	}
	idsParam := strings.Join(ids, ",")
	q := fmt.Sprintf("?limit=%d&ids=%s", limit, idsParam)
	body, _, err := a.daemonGet("/edges-bulk" + q)
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

// GetTasks pulls the project-management task list from the daemon.
func (a *App) GetTasks(status string, limit int) (any, error) {
	if limit <= 0 {
		limit = 200
	}
	q := fmt.Sprintf("?limit=%d", limit)
	if status != "" {
		q += "&status=" + status
	}
	body, _, err := a.daemonGet("/tasks" + q)
	if err != nil {
		return nil, err
	}
	var out any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetSkills pulls the available skill bundles + recent validations.
func (a *App) GetSkills() (map[string]any, error) {
	body, _, err := a.daemonGet("/skills")
	if err != nil {
		return nil, err
	}
	var skills any
	if err := json.Unmarshal(body, &skills); err != nil {
		return nil, err
	}
	progBody, _, err := a.daemonGet("/progress")
	if err != nil {
		return map[string]any{"skills": skills}, nil
	}
	var prog map[string]any
	_ = json.Unmarshal(progBody, &prog)
	return map[string]any{
		"skills":            skills,
		"validations_count": prog["validations_count"],
		"validated_skills":  prog["validated_skills"],
	}, nil
}

// UpdateTaskStatus PATCHes /tasks/{id} to change a task's workflow state.
func (a *App) UpdateTaskStatus(taskID string, status string) (map[string]any, error) {
	body := []byte(fmt.Sprintf(`{"status":%q}`, status))
	url := fmt.Sprintf("%s/tasks/%s", a.daemonURL, taskID)
	req, err := http.NewRequestWithContext(a.ctx, "PATCH", url,
		bytesReader(body))
	if err != nil {
		return nil, err
	}
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("update_task %s → %d: %s", taskID, resp.StatusCode, string(respBody))
	}
	var out map[string]any
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// DeleteNode hard-deletes a concept (used by Tasks for "drop task" action).
func (a *App) DeleteNode(conceptID string) (map[string]any, error) {
	url := fmt.Sprintf("%s/nodes/%s", a.daemonURL, conceptID)
	req, err := http.NewRequestWithContext(a.ctx, "DELETE", url, nil)
	if err != nil {
		return nil, err
	}
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("delete %s → %d: %s", conceptID, resp.StatusCode, string(respBody))
	}
	var out map[string]any
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetNodeNeighborhood returns linked concepts + edges + history + events
// for a single concept. Powers the task-detail side panel.
func (a *App) GetNodeNeighborhood(conceptID string) (map[string]any, error) {
	body, _, err := a.daemonGet("/nodes/" + conceptID + "/neighborhood")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// SourceStats returns per-source breakdown including cross-repo same_as
// link counts grouped by other_source_id. Powers the Sources detail
// "shared with N other sources" surface.
func (a *App) SourceStats(sourceID string) (map[string]any, error) {
	body, _, err := a.daemonGet("/sources/" + sourceID + "/stats")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// LinkCrossRepo runs the linker over every registered source and creates
// same_as edges between same-named symbols.
func (a *App) LinkCrossRepo(threshold float64) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{"threshold": threshold})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/sources/link", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": string(rb)}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// AddCodeSource registers a directory as a memex source via the daemon's
// POST /sources/add — single-writer-fronted-by-many-clients architecture
// avoids contesting the DuckDB writer lock with a separate engine.
func (a *App) AddCodeSource(path string, indexNow bool) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{
		"path":       path,
		"index_now":  indexNow,
	})
	req, err := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/sources/add", bytesReader(body))
	if err != nil {
		return nil, err
	}
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{
			"error":  fmt.Sprintf("daemon returned %d", resp.StatusCode),
			"detail": string(respBody),
			"path":   path,
		}, nil
	}
	var out map[string]any
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, err
	}
	out["ok"] = true
	return out, nil
}

// ReindexSource asks the daemon to drop + rebuild a source.
func (a *App) ReindexSource(sourceID string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/sources/"+sourceID+"/reindex", nil)
	if err != nil {
		return nil, err
	}
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		out["error"] = fmt.Sprintf("daemon returned %d", resp.StatusCode)
	}
	return out, nil
}

// RemoveSource deletes a source + every file/symbol it owns.
func (a *App) RemoveSource(sourceID string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(a.ctx, "DELETE",
		a.daemonURL+"/sources/"+sourceID, nil)
	if err != nil {
		return nil, err
	}
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		out["error"] = fmt.Sprintf("daemon returned %d", resp.StatusCode)
	}
	return out, nil
}

// ListCodeSources returns every kind=source concept (registered codebases).
func (a *App) ListCodeSources() (map[string]any, error) {
	body, _, err := a.daemonGet("/concepts?kind=source&limit=200")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// EnableAFK turns on AFK auto-approve for `durationHours` with a `note`.
func (a *App) EnableAFK(durationHours float64, note string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{
		"duration_hours": durationHours, "note": note,
	})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/afk/on", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// DisableAFK turns off AFK mode.
func (a *App) DisableAFK() (map[string]any, error) {
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/afk/off", nil)
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// AFKStatus returns the active AFK flag, or {active:false}.
func (a *App) AFKStatus() (map[string]any, error) {
	body, _, err := a.daemonGet("/afk")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

// PatchNode edits a concept in place. Patch fields:
// {name?, description?, kind?, confidence?, verification?, metadata_patch?}
func (a *App) PatchNode(conceptID string, patch map[string]any) (map[string]any, error) {
	body, _ := json.Marshal(patch)
	req, _ := http.NewRequestWithContext(a.ctx, "PATCH",
		a.daemonURL+"/nodes/"+conceptID, bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": fmt.Sprintf("daemon %d: %s", resp.StatusCode, string(rb))}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// CreateConcept makes a new concept via POST /nodes.
func (a *App) CreateConcept(name string, description string, kind string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{
		"name": name, "description": description, "kind": kind,
		"source": "human",
	})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/nodes", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": fmt.Sprintf("daemon %d: %s", resp.StatusCode, string(rb))}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// GetTaskComments fetches comments attached to a task.
func (a *App) GetTaskComments(taskID string) (map[string]any, error) {
	body, _, err := a.daemonGet("/tasks/" + taskID + "/comments")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

// AddTaskComment adds a comment / instruction to a task.
func (a *App) AddTaskComment(taskID string, text string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{"text": text, "actor": "human"})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/tasks/"+taskID+"/comments", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": string(rb)}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// FileSymbols lists every kind=symbol concept defined in a given file.
func (a *App) FileSymbols(sourceID string, fileID string) (map[string]any, error) {
	body, _, err := a.daemonGet(
		fmt.Sprintf("/sources/%s/files/%s/symbols", sourceID, fileID),
	)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

// ListSecretsViaDaemon hits /secrets — returns the index without values.
func (a *App) ListSecretsViaDaemon() (map[string]any, error) {
	body, _, err := a.daemonGet("/secrets")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// PutSecret stores a secret in the OS keychain via the daemon.
func (a *App) PutSecret(provider string, name string, value string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{
		"provider": provider, "name": name, "value": value,
	})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/secrets", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": string(rb)}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// DeleteSecret removes a secret by provider+name.
func (a *App) DeleteSecret(provider string, name string) (map[string]any, error) {
	req, _ := http.NewRequestWithContext(a.ctx, "DELETE",
		fmt.Sprintf("%s/secrets/%s/%s", a.daemonURL, provider, name), nil)
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// RedactPreview asks the daemon what auto-redact would do to a string.
// Does not store anything.
func (a *App) RedactPreview(text string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{"text": text})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/secrets/redact-preview", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// ListUpstreams returns the configured MCP upstreams.
func (a *App) ListUpstreams() (map[string]any, error) {
	body, _, err := a.daemonGet("/upstreams")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

// UpstreamCatalog returns the curated MCP catalog.
func (a *App) UpstreamCatalog() (map[string]any, error) {
	body, _, err := a.daemonGet("/upstreams/catalog")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	_ = json.Unmarshal(body, &out)
	return out, nil
}

// InstallUpstream installs a catalog entry into upstreams.json.
// Daemon restart is needed for the new upstream to actually start
// proxying tools — `restart_required:true` flag in the response.
func (a *App) InstallUpstream(catalogID string, name string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{
		"catalog_id": catalogID, "name": name,
	})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/upstreams/install", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": fmt.Sprintf("daemon %d: %s", resp.StatusCode, string(rb))}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// RemoveUpstream deletes an upstream from upstreams.json.
func (a *App) RemoveUpstream(name string) (map[string]any, error) {
	req, _ := http.NewRequestWithContext(a.ctx, "DELETE",
		a.daemonURL+"/upstreams/"+name, nil)
	a.applyAuth(req)
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": string(rb)}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// InstallSkill installs a built-in or registry skill bundle into memex.
func (a *App) InstallSkill(name string) (map[string]any, error) {
	body, _ := json.Marshal(map[string]any{"name": name})
	req, _ := http.NewRequestWithContext(a.ctx, "POST",
		a.daemonURL+"/skills/install", bytesReader(body))
	a.applyAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	rb, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return map[string]any{"error": fmt.Sprintf("daemon %d: %s", resp.StatusCode, string(rb))}, nil
	}
	var out map[string]any
	_ = json.Unmarshal(rb, &out)
	return out, nil
}

// GetHooksStatus reads ~/.claude/settings.json and reports whether memex
// hooks are wired into Claude Code. Used by the desktop Dashboard to
// distinguish "hooks not installed" from "hooks installed, no events yet".
func (a *App) GetHooksStatus() (map[string]any, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return map[string]any{"installed": false, "error": err.Error()}, nil
	}
	settingsPath := filepath.Join(home, ".claude", "settings.json")
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		return map[string]any{
			"installed": false,
			"reason":    "settings.json not found",
			"path":      settingsPath,
		}, nil
	}
	var settings map[string]any
	if err := json.Unmarshal(data, &settings); err != nil {
		return map[string]any{
			"installed": false,
			"reason":    "settings.json parse error",
			"error":     err.Error(),
		}, nil
	}
	hooks, _ := settings["hooks"].(map[string]any)
	wired := map[string]bool{}
	for ev, raw := range hooks {
		handlers, _ := raw.([]any)
		for _, h := range handlers {
			hm, _ := h.(map[string]any)
			inner, _ := hm["hooks"].([]any)
			for _, i := range inner {
				im, _ := i.(map[string]any)
				cmd, _ := im["command"].(string)
				if strings.Contains(cmd, "memex") {
					wired[ev] = true
				}
			}
		}
	}
	return map[string]any{
		"installed":     len(wired) > 0,
		"events_wired":  wired,
		"settings_path": settingsPath,
	}, nil
}

// GetSchema returns the DuckDB schema (tables + columns + row counts).
func (a *App) GetSchema() (map[string]any, error) {
	body, _, err := a.daemonGet("/schema")
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// GetTableRows returns a paginated read-only sample of rows from a table.
func (a *App) GetTableRows(table string, limit int, offset int) (map[string]any, error) {
	if limit <= 0 {
		limit = 50
	}
	q := fmt.Sprintf("?limit=%d&offset=%d", limit, offset)
	body, _, err := a.daemonGet("/schema/" + table + "/rows" + q)
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
