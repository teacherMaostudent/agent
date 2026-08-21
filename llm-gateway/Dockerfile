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
# 使用当前进程的 Actuator 探针。不能再次执行 app.jar；后者会在每次探测时启动一个
# 新 Spring 进程，既永远超时，又会重复连接数据库和 Redis。
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
  CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /actuator/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 && head -n 1 <&3 | grep -q ' 200 '"]
ENTRYPOINT ["java", "-jar", "/service/app.jar"]
