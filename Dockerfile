FROM golang:1.24-alpine AS builder
WORKDIR /app
COPY go.mod ./
COPY main.go ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -ldflags="-s -w -X main.version=${VERSION}" -o /demo-app .

FROM gcr.io/distroless/static-debian12
COPY --from=builder /demo-app /demo-app
EXPOSE 8080
ENTRYPOINT ["/demo-app"]
