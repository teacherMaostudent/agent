FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build
COPY pom.xml ./
RUN mvn -q -DskipTests dependency:go-offline
COPY src ./src
RUN mvn -q -DskipTests package

FROM eclipse-temurin:21-jre
WORKDIR /service
COPY --from=build /build/target/llm-gateway-*.jar /service/app.jar
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD ["java", "-jar", "/service/app.jar", "--management.endpoint.health.probes.enabled=true"]
ENTRYPOINT ["java", "-jar", "/service/app.jar"]
