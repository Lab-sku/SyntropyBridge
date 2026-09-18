package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/config"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/gateway"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider"
	"github.com/Lab-sku/SyntropyBridge/gateway-go/internal/provider/openaicompat"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	cfg, err := config.FromEnv()
	if err != nil {
		logger.Error("invalid gateway configuration", "error", err)
		os.Exit(2)
	}

	registry := provider.NewRegistry()
	if err := registry.Register(openaicompat.New()); err != nil {
		logger.Error("register provider adapter", "error", err)
		os.Exit(2)
	}
	gatewayServer := gateway.NewServer(logger, registry)
	httpServer := &http.Server{
		Addr:              cfg.Addr,
		Handler:           gatewayServer.Handler(),
		ReadHeaderTimeout: cfg.ReadHeaderTimeout,
		IdleTimeout:       cfg.IdleTimeout,
	}

	serverErrors := make(chan error, 1)
	go func() {
		logger.Info("starting SyntropyBridge gateway", "addr", cfg.Addr)
		serverErrors <- httpServer.ListenAndServe()
	}()

	signalContext, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	select {
	case <-signalContext.Done():
		logger.Info("shutdown signal received")
	case err := <-serverErrors:
		if !errors.Is(err, http.ErrServerClosed) {
			logger.Error("gateway server stopped unexpectedly", "error", err)
			os.Exit(1)
		}
	}

	shutdownContext, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := httpServer.Shutdown(shutdownContext); err != nil {
		logger.Error("graceful shutdown failed", "error", err)
		os.Exit(1)
	}
	logger.Info("gateway stopped")
}
