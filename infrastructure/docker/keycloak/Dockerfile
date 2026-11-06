# Build Keycloak's settings into the image, so it starts quickly on a read-only filesystem.
#   docker build -t windops-keycloak:local infrastructure/docker/keycloak
FROM quay.io/keycloak/keycloak:26.8.1@sha256:3f3c5f8e03c614774b4bd9173b88391fefa4153bfc010d8012d59ad825377332
# Under /auth, behind the gateway. Health and metrics stay at / on the management port.
RUN /opt/keycloak/bin/kc.sh build --db=postgres --health-enabled=true --metrics-enabled=true --event-metrics-user-enabled=true \
    --http-relative-path=/auth --http-management-relative-path=/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized"]
