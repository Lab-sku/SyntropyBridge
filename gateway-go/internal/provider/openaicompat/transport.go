package openaicompat

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
)

func chatEndpoint(deployment provider.Deployment) (string, error) {
	base := strings.TrimSpace(deployment.BaseURL)
	if base == "" {
		return "", errors.New("build OpenAI-compatible request: deployment base URL is empty")
	}
	parsed, err := url.Parse(base)
	if err != nil {
		return "", fmt.Errorf("build OpenAI-compatible request: invalid base URL: %w", err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("build OpenAI-compatible request: unsupported URL scheme %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return "", errors.New("build OpenAI-compatible request: base URL host is empty")
	}
	path := deployment.Metadata["chat_path"]
	if path == "" {
		path = defaultChatPath
	}
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	basePath := strings.TrimRight(parsed.Path, "/")
	if strings.HasSuffix(basePath, "/v1") && strings.HasPrefix(path, "/v1/") {
		path = strings.TrimPrefix(path, "/v1")
	}
	parsed.Path = basePath + path
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String(), nil
}

func applyCredential(headers http.Header, deployment provider.Deployment, credential provider.Credential) {
	if credential.Secret == "" {
		return
	}
	headerName := deployment.Metadata["auth_header"]
	if headerName == "" {
		headerName = "Authorization"
	}
	if headers.Get(headerName) != "" {
		return
	}
	scheme := deployment.Metadata["auth_scheme"]
	if scheme == "" && strings.EqualFold(headerName, "Authorization") {
		scheme = "Bearer"
	}
	value := credential.Secret
	if scheme != "" {
		value = scheme + " " + credential.Secret
	}
	headers.Set(headerName, value)
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	limited := io.LimitReader(reader, limit+1)
	body, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("body exceeds %d bytes", limit)
	}
	return body, nil
}
