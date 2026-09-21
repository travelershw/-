cmake_minimum_required(VERSION 3.24)
project(qingyu_legacy LANGUAGES C CXX)
if(NOT WIN32)
  message(FATAL_ERROR "The legacy bot uses Windows APIs and requires Windows.")
endif()
include(FetchContent)
set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(ENABLE_PROGRAMS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(mbedtls
  GIT_REPOSITORY https://github.com/Mbed-TLS/mbedtls.git
  GIT_TAG v3.6.3
  GIT_SHALLOW TRUE
  GIT_SUBMODULES_RECURSE TRUE)
FetchContent_MakeAvailable(mbedtls)
add_executable(qingyu_legacy mian.cpp)
target_compile_features(qingyu_legacy PRIVATE cxx_std_17)
target_compile_definitions(qingyu_legacy PRIVATE CPPHTTPLIB_MBEDTLS_SUPPORT)
if(MSVC)
  target_compile_options(qingyu_legacy PRIVATE /utf-8)
endif()
target_link_libraries(qingyu_legacy PRIVATE mbedtls mbedx509 mbedcrypto bcrypt ws2_32 crypt32 wsock32)
