idf_component_register(SRCS "bootloader_start.c" "iap_bootloader.c"
                       REQUIRES bootloader bootloader_support spi_flash
                                esp_app_format esp_rom hal iap_protocol)

# Keep the standard ESP-IDF bootloader linker layout.
idf_build_get_property(scripts BOOTLOADER_LINKER_SCRIPT)
target_linker_script(${COMPONENT_LIB} INTERFACE "${scripts}")
