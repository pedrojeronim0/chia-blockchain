FROM chia-private:local

# Keep the standalone Compose deployment usable with the same base image.
COPY docker/private-network/configure.py /app/docker/private-network/configure.py
COPY docker/private-network/kollaps/entrypoint.sh /app/kollaps-entrypoint.sh
ENTRYPOINT ["bash", "/app/kollaps-entrypoint.sh"]
