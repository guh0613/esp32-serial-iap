set(SHARED_IAP_PROTOCOL_DIR
    "${CMAKE_CURRENT_LIST_DIR}/../../components/iap_protocol")

idf_component_register(
    SRCS "${SHARED_IAP_PROTOCOL_DIR}/iap_protocol.c"
    INCLUDE_DIRS "${SHARED_IAP_PROTOCOL_DIR}/include")
