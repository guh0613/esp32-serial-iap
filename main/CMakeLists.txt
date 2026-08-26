idf_component_register(SRCS "main.c" "iap_service.c"
                       INCLUDE_DIRS "."
                       REQUIRES app_update esp_driver_uart esp_rom esp_timer
                                iap_protocol mbedtls)
