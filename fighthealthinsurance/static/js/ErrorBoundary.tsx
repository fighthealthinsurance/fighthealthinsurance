import React, { Component, ErrorInfo, ReactNode } from "react";
import { Alert, Box, Button, Stack, Text, Title } from "@mantine/core";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  componentName?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

/**
 * Error boundary component that catches JavaScript errors anywhere in the child
 * component tree and displays a fallback UI instead of crashing the whole app.
 *
 * Usage:
 * ```tsx
 * <ErrorBoundary componentName="MyComponent">
 *   <MyComponent />
 * </ErrorBoundary>
 * ```
 */
class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    // Update state so the next render will show the fallback UI
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // Log the error to console for debugging
    console.error("ErrorBoundary caught an error:", error, errorInfo);

    this.setState({ errorInfo });

    // Report to Sentry if available
    if (typeof window !== "undefined" && (window as any).Sentry) {
      (window as any).Sentry.captureException(error, {
        extra: {
          componentName: this.props.componentName,
          componentStack: errorInfo.componentStack,
        },
      });
    }
  }

  handleRetry = (): void => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render(): ReactNode {
    if (this.state.hasError) {
      // Custom fallback UI if provided
      if (this.props.fallback) {
        return this.props.fallback;
      }

      // Default fallback UI
      return (
        <Box p="md">
          <Alert color="red" title="Something went wrong" radius="md">
            <Stack gap="sm">
              <Text size="sm">
                {this.props.componentName
                  ? `An error occurred in ${this.props.componentName}.`
                  : "An unexpected error occurred."}
              </Text>
              <Text size="xs" c="dimmed">
                {this.state.error?.message || "Unknown error"}
              </Text>
              <Button
                size="xs"
                variant="light"
                color="red"
                onClick={this.handleRetry}
              >
                Try Again
              </Button>
            </Stack>
          </Alert>
        </Box>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
